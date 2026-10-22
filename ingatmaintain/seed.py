"""Built-in checklist templates and the NHG Khatib Polyclinic example data."""

from .views.procedures import save_procedure

CLIENT = "NHG Khatib Polyclinic"
PROCEDURE = "SSL certificate renewal - MQTT (Mosquitto)"

SSL_STEPS = [
    # (section, title, details, per computer?)
    ("Preparation", "Agree the visit date with the clinic and confirm site access",
     "Check registration / escort requirements and permitted working hours.", False),
    ("Preparation", "Confirm the Synapxe contact who will submit the CSRs and their turnaround time",
     "Ask whether they can return the .cer files the same day. If not, plan a second visit "
     "for the install (add it on the job page).", False),
    ("Preparation", "Check laptop software and pack every item on the packing list", "", False),

    ("On site - generate CSR", "Generate a CSR on the computer",
     "Keep the pending request in Certificate Manager - the private key stays on this computer "
     "and is needed when the .cer is installed.", True),
    ("On site - generate CSR", "Copy the CSR to the Lenovo secure drive",
     "Name each file after the computer so the returned .cer files can be matched.", True),

    ("Synapxe", "Hand the CSR files to Synapxe for submission", "Log the reference / ticket number in the job notes.", False),
    ("Synapxe", "Receive the signed .cer files from Synapxe",
     "Check there is one .cer per computer and note the new expiry date.", False),

    ("On site - install certificate", "Install the .cer file on the computer",
     "Completes the pending request in Certificate Manager.", True),
    ("On site - install certificate", "Export the certificate with private key from Certificate Manager to the secure drive",
     "Export as .pfx including the private key.", True),
    ("On site - install certificate", "Extract the private key with OpenSSL",
     "openssl pkcs12 -in <computer>.pfx -nocerts -nodes -out <computer>.key\n"
     "openssl pkcs12 -in <computer>.pfx -clcerts -nokeys -out <computer>.crt", True),
    ("On site - install certificate", "Put the certificate and key files in the Mosquitto certificate path",
     "Use the paths set by certfile / keyfile in mosquitto.conf.", True),
    ("On site - install certificate", "Restart Mosquitto and confirm MQTT clients connect over TLS", "", True),

    ("Wrap up", "Wipe the private keys and .pfx files from the secure drive", "", False),
    ("Wrap up", "Record the new certificate expiry date (asked for when completing this job)", "", False),
]

SSL_SOFTWARE = [
    ("OpenSSL", "To extract the private key from the exported .pfx"),
    ("Text editor (e.g. Notepad++ / VS Code)", "To check certificate paths in mosquitto.conf"),
    ("MQTT client (e.g. MQTT Explorer or mosquitto_sub)", "To test the TLS connection after installing"),
]

SSL_EQUIPMENT = [
    ("Lenovo secure drive", "1", "Encrypted USB for CSR, .cer and .pfx files"),
    ("Portable monitor", "1", ""),
    ("Portable keyboard", "1", ""),
    ("Mouse", "1", ""),
    ("HDMI / USB-C cables and adapters", "", "For the portable monitor"),
    ("Laptop and charger", "1", ""),
    ("Staff pass / site access authorisation", "", ""),
]


DEPLOY_PROCEDURE = "Software deployment / upgrade"

DEPLOY_STEPS = [
    ("Preparation", "Confirm the version to deploy and read the release notes",
     "Check for config changes, database migrations or new prerequisites.", False),
    ("Preparation", "Get the client's approval and agree the change window",
     "Note the approval / change request reference in the job notes.", False),
    ("Preparation", "Copy the installer to the secure drive and check its checksum",
     "Windows: certutil -hashfile <installer> SHA256", False),
    ("Preparation", "Agree the visit date with the client and confirm site access", "", False),
    ("Preparation", "Check laptop software and pack every item on the packing list", "", False),

    ("On site", "Check the installed version matches IngatMaintain",
     "If it doesn't, record the actual version first so the history stays correct.", True),
    ("On site", "Back up the configuration and data", "", True),
    ("On site", "Stop the service / close the application", "", True),
    ("On site", "Install the new version", "", True),
    ("On site", "Check the configuration carried over", "", True),
    ("On site", "Start the service and do a quick functional test", "", True),
    ("On site", "Confirm with clinic staff that everything works", "", False),

    ("Wrap up", "Record the new versions in IngatMaintain",
     "Use Record versions on this job page - fill in 'All computers' if every machine got the same version.", False),
    ("Wrap up", "Remove installers and client data from the secure drive", "Keep backups only where policy allows.", False),
    ("Wrap up", "Note any issues or follow-ups in the job notes", "", False),
]

DEPLOY_SOFTWARE = [
    ("Installer for the new version", "Checksum verified"),
    ("Text editor (e.g. Notepad++ / VS Code)", "To compare configuration files"),
]

DEPLOY_EQUIPMENT = [
    ("Lenovo secure drive", "1", "Installer and backups"),
    ("Portable monitor", "1", ""),
    ("Portable keyboard", "1", ""),
    ("Mouse", "1", ""),
    ("HDMI / USB-C cables and adapters", "", "For the portable monitor"),
    ("Laptop and charger", "1", ""),
    ("Staff pass / site access authorisation", "", ""),
]

# key: (name, description, steps, software, equipment)
TEMPLATES = {
    "ssl-mqtt": (PROCEDURE, "Renew the SSL certificates used by Mosquitto on the clinic computers. "
                            "CSRs are submitted and signed through Synapxe.",
                 SSL_STEPS, SSL_SOFTWARE, SSL_EQUIPMENT),
    "software-deployment": (DEPLOY_PROCEDURE, "Install a new version of a product on a client's computers "
                                              "and record the versions afterwards.",
                            DEPLOY_STEPS, DEPLOY_SOFTWARE, DEPLOY_EQUIPMENT),
}


def ensure_template(conn, key):
    """Create a built-in checklist if no checklist with its name exists yet. Returns the checklist id."""
    name, description, steps, software, equipment = TEMPLATES[key]
    row = conn.execute("SELECT id FROM procedures WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return save_procedure(
        conn,
        {"name": name, "description": description},
        [{"section": s, "title": t, "details": d, "per_machine": int(pm)} for s, t, d, pm in steps],
        [{"name": n, "details": d} for n, d in software],
        [{"name": n, "quantity": q, "details": d} for n, q, d in equipment],
    )


def seed_example(conn):
    if conn.execute("SELECT 1 FROM clients WHERE name = ?", (CLIENT,)).fetchone():
        return f"{CLIENT} already exists - nothing loaded."

    procedure_id = ensure_template(conn, "ssl-mqtt")
    ensure_template(conn, "software-deployment")

    client_id = conn.execute(
        "INSERT INTO clients (name, access_notes) VALUES (?, ?)",
        (CLIENT, "Certificates are signed via Synapxe - allow time for their turnaround."),
    ).lastrowid
    conn.executemany(
        "INSERT INTO machines (client_id, name, os) VALUES (?, ?, ?)",
        [(client_id, f"Computer {n}", "Windows") for n in range(1, 6)],
    )
    conn.execute(
        "INSERT INTO maintenance_items (client_id, procedure_id, title, category, due_date, reminder_days, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (client_id, procedure_id, "SSL certificate - MQTT (Mosquitto)", "SSL certificate", "2028-10-21",
         "180,120,90,60,30,14,7,1",
         "Installed on all five computers. Reminders start 6 months out because Synapxe has to sign the CSRs."),
    )
    conn.commit()
    return f"Loaded {CLIENT}: 5 computers, SSL certificate expiring 21 Oct 2028, and the renewal checklist."
