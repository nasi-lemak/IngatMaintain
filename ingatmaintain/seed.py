"""Example data: the NHG Khatib Polyclinic MQTT SSL certificate renewal."""

from .views.procedures import save_procedure

CLIENT = "NHG Khatib Polyclinic"
PROCEDURE = "SSL certificate renewal - MQTT (Mosquitto)"

SSL_STEPS = [
    # (section, title, details, per computer?)
    ("Preparation", "Agree the visit date with the clinic and confirm site access",
     "Check registration / escort requirements and permitted working hours.", False),
    ("Preparation", "Confirm the Synapxe contact who will submit the CSRs and their turnaround time",
     "Book the second visit only after the .cer files are expected back.", False),
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


def seed_example(conn):
    if conn.execute("SELECT 1 FROM clients WHERE name = ?", (CLIENT,)).fetchone():
        return f"{CLIENT} already exists - nothing loaded."

    proc = conn.execute("SELECT id FROM procedures WHERE name = ?", (PROCEDURE,)).fetchone()
    if proc:
        procedure_id = proc["id"]
    else:
        procedure_id = save_procedure(
            conn,
            {"name": PROCEDURE, "description": "Renew the SSL certificates used by Mosquitto on the clinic computers. "
                                               "CSRs are submitted and signed through Synapxe."},
            [{"section": s, "title": t, "details": d, "per_machine": int(pm)} for s, t, d, pm in SSL_STEPS],
            [{"name": n, "details": d} for n, d in SSL_SOFTWARE],
            [{"name": n, "quantity": q, "details": d} for n, q, d in SSL_EQUIPMENT],
        )

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
