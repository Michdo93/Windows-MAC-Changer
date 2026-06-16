import winreg
import subprocess
import re
import sys
import time
import ctypes

# Registry-Pfad für Netzwerkadapter
REG_PATH = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"


def is_admin():
    """Prüft ob das Skript als Administrator läuft."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def get_network_adapters():
    """Liest alle physischen Netzwerkadapter aus der Registry."""
    adapters = []
    skip_keywords = ["WAN Miniport", "Virtual", "Kernel Debug", "Bluetooth",
                     "OpenVPN", "TAP-Windows", "UsbNcm"]
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_PATH) as key:
            for i in range(200):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name, 0, winreg.KEY_READ) as subkey:
                        try:
                            name, _ = winreg.QueryValueEx(subkey, "DriverDesc")
                            adapter_id, _ = winreg.QueryValueEx(subkey, "NetCfgInstanceId")

                            if any(kw.lower() in name.lower() for kw in skip_keywords):
                                continue

                            adapters.append({
                                "index": subkey_name,
                                "name": name,
                                "id": adapter_id
                            })
                        except FileNotFoundError:
                            continue
                except OSError:
                    break
    except Exception as e:
        print(f"[-] Fehler beim Lesen der Registry: {e}")
        sys.exit(1)
    return adapters


def write_mac_to_registry(adapter_index, adapter_guid, new_mac):
    """
    Schreibt die MAC an ZWEI Stellen in die Registry:
    1. Treiber-Klasse (NetworkAddress)
    2. Netzwerk-Konfiguration (NetworkAddress)
    """
    success = True

    # Stelle 1: Treiber-Klasse
    adapter_path = rf"{REG_PATH}\{adapter_index}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, adapter_path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "NetworkAddress", 0, winreg.REG_SZ, new_mac)
        print(f"[+] Registry (Treiber-Klasse) aktualisiert.")
    except PermissionError:
        print("[-] FEHLER: Kein Schreibzugriff auf Registry (kein Admin?).")
        return False
    except Exception as e:
        print(f"[-] Fehler beim Schreiben (Treiber-Klasse): {e}")
        success = False

    # Stelle 2: Netzwerk-Konfiguration
    net_path = rf"SYSTEM\CurrentControlSet\Control\Network\{{4D36E972-E325-11CE-BFC1-08002BE10318}}\{adapter_guid}\Connection"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, net_path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "NetworkAddress", 0, winreg.REG_SZ, new_mac)
        print(f"[+] Registry (Netzwerk-Config) aktualisiert.")
    except FileNotFoundError:
        pass  # Dieser Pfad existiert nicht immer
    except Exception as e:
        print(f"[!] Hinweis: Netzwerk-Config-Pfad nicht beschreibbar: {e}")

    return success


def get_interface_name_by_guid(guid):
    """Ermittelt den Verbindungsnamen (z.B. 'WLAN') anhand der GUID."""
    cmd = f"(Get-NetAdapter | Where-Object {{ $_.InterfaceGuid -eq '{guid}' }}).Name"
    try:
        result = subprocess.run(
            ["powershell", "-Command", cmd],
            capture_output=True, text=True, check=True
        )
        name = result.stdout.strip()
        return name if name else None
    except subprocess.CalledProcessError:
        return None


def hard_reset_adapter(interface_name, guid):
    """
    Führt einen harten Reset durch:
    1. Adapter per PowerShell deaktivieren
    2. Treiber per devcon neu laden (wenn verfügbar)
    3. Adapter wieder aktivieren
    """
    print(f"[*] Führe harten Reset für '{interface_name}' durch...")

    # Schritt 1: Deaktivieren
    ps_disable = f"Disable-NetAdapter -Name '{interface_name}' -Confirm:$false"
    r = subprocess.run(["powershell", "-Command", ps_disable], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[-] Deaktivieren fehlgeschlagen: {r.stderr.strip()}")
        return False
    print("[*] Adapter deaktiviert. Warte 4 Sekunden...")
    time.sleep(4)

    # Schritt 2: Versuche Treiber-Neustart via devcon (optional)
    _try_devcon_restart(guid)

    # Schritt 3: Aktivieren
    ps_enable = f"Enable-NetAdapter -Name '{interface_name}' -Confirm:$false"
    r = subprocess.run(["powershell", "-Command", ps_enable], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[-] Aktivieren fehlgeschlagen: {r.stderr.strip()}")
        return False

    print("[*] Adapter aktiviert. Warte 3 Sekunden auf Initialisierung...")
    time.sleep(3)
    return True


def _try_devcon_restart(guid):
    """Versucht einen Treiber-Reset via devcon.exe (optional)."""
    try:
        result = subprocess.run(
            ["devcon.exe", "restart", f"=net @*{guid}*"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print("[+] Treiber via devcon neu gestartet.")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # devcon nicht installiert – kein Problem


def verify_mac(interface_name, expected_mac):
    """Prüft ob die neue MAC wirklich aktiv ist."""
    clean = re.sub(r'[^A-F0-9]', '', expected_mac.upper())
    expected_fmt = '-'.join(clean[i:i+2] for i in range(0, 12, 2))

    cmd = f"(Get-NetAdapter | Where-Object {{ $_.Name -eq '{interface_name}' }}).MacAddress"
    try:
        result = subprocess.run(
            ["powershell", "-Command", cmd],
            capture_output=True, text=True, timeout=10
        )
        current = result.stdout.strip().upper()

        if not current:
            print("[-] Konnte aktuelle MAC nicht auslesen.")
            return False

        if current == expected_fmt:
            print(f"\n[✓] ERFOLG! MAC ist jetzt: {current}")
            return True
        else:
            print(f"\n[-] MAC wurde NICHT geändert!")
            print(f"    Erwartet : {expected_fmt}")
            print(f"    Aktuell  : {current}")
            return False
    except Exception as e:
        print(f"[-] Fehler bei Verifikation: {e}")
        return False


def check_driver_supports_mac_change(adapter_index):
    """
    Prüft ob der Treiber einen 'NetworkAddress'-Eintrag in den
    erweiterten Eigenschaften hat (= MAC-Spoofing wird unterstützt).
    """
    adapter_path = rf"{REG_PATH}\{adapter_index}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, adapter_path, 0, winreg.KEY_READ) as key:
            try:
                with winreg.OpenKey(key, r"Ndi\params\NetworkAddress"):
                    print("[+] Treiber unterstützt 'NetworkAddress' (MAC-Spoofing).")
                    return True
            except FileNotFoundError:
                print("[!] Warnung: Treiber hat keinen 'NetworkAddress'-Parameter.")
                print("    MAC-Änderung könnte vom Treiber ignoriert werden.")
                return False
    except Exception:
        return False


# ===========================================================================
#  HAUPTPROGRAMM
# ===========================================================================
if __name__ == "__main__":
    print("=== Windows MAC-Address Changer (v2) ===\n")

    # Admin-Check
    if not is_admin():
        print("[-] FEHLER: Dieses Skript muss als ADMINISTRATOR ausgeführt werden!")
        print("    Rechtsklick auf CMD → 'Als Administrator ausführen'.")
        sys.exit(1)
    print("[+] Administrator-Rechte erkannt.\n")

    # 1. Adapter auflisten (gefiltert, ohne virtuelle/WAN-Adapter)
    adapters = get_network_adapters()
    if not adapters:
        print("[-] Keine physischen Netzwerkadapter gefunden.")
        sys.exit()

    print("Verfügbare Netzwerkadapter:")
    for idx, adapter in enumerate(adapters):
        print(f"  [{idx}] {adapter['name']}  (Reg-Key: {adapter['index']})")

    # 2. Adapter wählen
    try:
        choice = int(input("\nNummer des Adapters wählen: "))
        selected = adapters[choice]
    except (ValueError, IndexError):
        print("[-] Ungültige Auswahl.")
        sys.exit()

    print(f"\n[*] Gewählt: {selected['name']}")
    print(f"    GUID   : {selected['id']}")

    # 3. Treiber-Unterstützung prüfen
    supported = check_driver_supports_mac_change(selected["index"])
    if not supported:
        antwort = input("\n    Trotzdem fortfahren? (j/n): ").strip().lower()
        if antwort != 'j':
            print("[-] Abbruch.")
            sys.exit()

    # 4. Neue MAC eingeben
    print("\nFormat: 12 Hex-Zeichen, KEINE Trennzeichen (z.B. 02AA00BBCCDD)")
    print("WLAN  : 2. Zeichen muss 2, 6, A oder E sein (locally administered)!")
    new_mac_input = input("Neue MAC-Adresse: ").strip().upper()

    clean_mac = re.sub(r'[^A-F0-9]', '', new_mac_input)
    if len(clean_mac) != 12:
        print("[-] Ungültiges Format. Genau 12 Hexadezimalzeichen erforderlich.")
        sys.exit()

    # WLAN-Bit prüfen
    second_char = clean_mac[1]
    if second_char not in ('2', '6', 'A', 'E'):
        print(f"[!] Warnung: Das 2. Zeichen '{second_char}' ist kein locally-administered Bit.")
        print("    Bei WLAN-Adaptern kann das dazu führen, dass die MAC abgelehnt wird.")
        antwort = input("    Trotzdem fortfahren? (j/n): ").strip().lower()
        if antwort != 'j':
            sys.exit()

    # 5. Verbindungsnamen ermitteln
    interface_name = get_interface_name_by_guid(selected["id"])
    if not interface_name:
        print("[-] Verbindungsname konnte nicht automatisch ermittelt werden.")
        print("    Tipp: 'getmac /v /fo list' → Spalte 'Verbindungsname' (z.B. WLAN)")
        interface_name = input("Verbindungsname manuell eingeben: ").strip()
        if not interface_name:
            print("[-] Kein Name angegeben. Abbruch.")
            sys.exit()

    print(f"[+] Verbindungsname: '{interface_name}'")

    # 6. MAC in Registry schreiben
    if not write_mac_to_registry(selected["index"], selected["id"], clean_mac):
        print("[-] Registry-Schreibfehler. Abbruch.")
        sys.exit()

    # 7. Harten Adapter-Reset durchführen
    if not hard_reset_adapter(interface_name, selected["id"]):
        print("\n[-] Automatischer Neustart fehlgeschlagen.")
        print(f"    Bitte '{interface_name}' manuell deaktivieren und wieder aktivieren.")
        sys.exit()

    # 8. Verifizieren
    success = verify_mac(interface_name, clean_mac)

    if not success:
        print("\n[!] Der Treiber ignoriert den Registry-Wert.")
        print("    Mögliche Lösungen:")
        print()
        formatted = '-'.join(clean_mac[i:i+2] for i in range(0, 12, 2))
        print("    A) Manuell im Geräte-Manager setzen:")
        print("       Geräte-Manager → Adapter → Eigenschaften → Reiter 'Erweitert'")
        print(f"       → 'Netzwerkadresse' oder 'Locally Administered Address' → Wert: {formatted}")
        print()
        print("    B) Technitium MAC Address Changer (kostenlos, arbeitet auf NDIS-Ebene):")
        print("       https://technitium.com/tmac/")
    else:
        print("[*] Prüfe mit: getmac /v /fo list")
