"""
PA Transfer Orchestrator v2
============================
Full human-in-the-loop web app for Walmart Business Primary Admin transfers.
- Reads live from Google Sheets (no file upload needed)
- Writes Interaction_History + Simulation_Log back to Google Sheet via gspread
- Automatic rules engine triage with proof of work
- Editable Email + SMS + Voicemail drafts before sending
- Agent approval required before any communication fires
"""

import streamlit as st
import pandas as pd
import re
from datetime import datetime
import io
import json

# ── Safe-load API clients ─────────────────────────────────────────────────────
try:
    from twilio.rest import Client as TwilioClient
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    APIS_AVAILABLE = True
except ImportError:
    APIS_AVAILABLE = False

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# ── Google Sheet config ───────────────────────────────────────────────────────
SHEET_ID     = "1aTY9CICa-jEsIOLKNhimUT78YxqreKG3"
GVIZ_BASE    = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet="
SHEET_NAMES  = ["Database", "Email_Templates", "Interaction_History", "Simulation_Log"]
SCOPES       = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PA Transfer Orchestrator",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background-color: #f8f9fa; }
    .flag-card {
        background: #fff3cd; border-left: 4px solid #ffc107;
        padding: 10px 16px; border-radius: 4px; margin: 6px 0;
        font-size: 14px;
    }
    .clear-card {
        background: #d1e7dd; border-left: 4px solid #198754;
        padding: 10px 16px; border-radius: 4px; margin: 6px 0;
        font-size: 14px;
    }
    .rule-badge {
        background: #0d6efd; color: white;
        padding: 4px 12px; border-radius: 20px;
        font-size: 13px; font-weight: 600;
    }
    .stage-header {
        font-size: 22px; font-weight: 700; color: #212529;
        border-bottom: 2px solid #0d6efd; padding-bottom: 8px;
        margin-bottom: 16px;
    }
    .gs-badge {
        background: #34a853; color: white;
        padding: 3px 10px; border-radius: 12px;
        font-size: 12px; font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS LAYER
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=120)          # re-fetch every 2 minutes
def load_sheet_tab(tab_name: str) -> pd.DataFrame:
    """Load a single tab from Google Sheet via public CSV export (gviz endpoint)."""
    url = GVIZ_BASE + tab_name.replace(" ", "%20")
    return pd.read_csv(url)


def load_all_sheets():
    """Load all four tabs. Returns (db, templates, history, sim) DataFrames."""
    db        = load_sheet_tab("Database")
    templates = load_sheet_tab("Email_Templates")
    history   = load_sheet_tab("Interaction_History")
    sim       = load_sheet_tab("Simulation_Log")
    return db, templates, history, sim


def get_gspread_client():
    """Authenticate gspread using service account JSON stored in Streamlit secrets."""
    if not GSPREAD_AVAILABLE:
        return None
    try:
        sa_info = dict(st.secrets["gcp_service_account"])
        creds   = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception:
        return None


def append_to_sheet(tab_name: str, row_dict: dict):
    """Append a row to the named sheet tab via gspread. Returns True on success."""
    gc = get_gspread_client()
    if gc is None:
        return False
    try:
        sh  = gc.open_by_key(SHEET_ID)
        ws  = sh.worksheet(tab_name)
        # Match column order to existing headers
        headers = ws.row_values(1)
        row_values = [str(row_dict.get(h, "")) for h in headers]
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        st.warning(f"Could not write to Google Sheet: {e}")
        return False


def clear_sheet_cache():
    """Force reload of sheet data."""
    load_sheet_tab.clear()


# ══════════════════════════════════════════════════════════════════════════════
# RULES ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_rules_engine(row, templates_df):
    """Determine the correct template and rule based on record flags."""
    def yn(col): return str(row.get(col, "No")).strip().lower() == "yes"
    def na(col): return str(row.get(col, "N/A")).strip().upper() == "N/A"

    has_account   = yn("Incoming PA existing account")
    is_pbi        = yn("Org_PBI_Status")
    is_tax        = yn("Outgoing PA Tax Exempt")
    has_phys_card = yn("Outgoing PA physical card") and not na("Outgoing PA physical card")

    flags, clears = [], []

    if not has_account:
        template_name = "Ask for Proof"
        rule = "Rule 01 — Incoming PA has no existing WB account. Written proof required before any transfer."
        flags.append("❗ Incoming PA does NOT have an existing WB account")
    elif is_pbi and is_tax and has_phys_card:
        template_name = "PBI (Physical Card) + Tax Considerations"
        rule = "Rule 14+22 — PBI active (with physical card) AND Tax-Exempt. Both considerations apply."
        flags += ["⚠️ PBI active — TreviPay update required (2–3 days)",
                  "⚠️ Tax-Exempt — must deactivate and re-enroll",
                  "⚠️ Physical card present — 3-day deactivation notice"]
    elif is_pbi and is_tax:
        template_name = "PBI (No Active Physical Card) and Tax Considerations"
        rule = "Rule 14+22 — PBI active (no physical card) AND Tax-Exempt. Both considerations apply."
        flags += ["⚠️ PBI active — TreviPay update required (2–3 days)",
                  "⚠️ Tax-Exempt — must deactivate and re-enroll"]
        clears.append("✅ No active physical card")
    elif is_pbi:
        template_name = "PBI Only (No Active Physical Card)"
        rule = "Rule 22 — PBI active. TreviPay update required post-transfer."
        flags.append("⚠️ PBI active — TreviPay update required (2–3 days)")
        clears.append("✅ No tax-exempt flag")
    elif is_tax:
        template_name = "Tax-Exempt Only Considerations"
        rule = "Rule 14 — Tax-Exempt account. Must deactivate and re-enroll after transfer."
        flags.append("⚠️ Tax-Exempt — must deactivate and re-enroll")
        clears.append("✅ No PBI flag")
    else:
        template_name = "Setting up for a Meeting / Call"
        rule = "Rule 00 — Standard transfer. No special flags. Schedule call to proceed."
        clears += ["✅ No PBI flag", "✅ No Tax-Exempt flag", "✅ Incoming PA has existing account"]

    trow = templates_df[templates_df["Template_Name"] == template_name]
    subject       = trow["Default_Subject"].values[0] if not trow.empty else "Walmart Business Update"
    template_body = trow["Body_Text"].values[0]       if not trow.empty else ""

    return template_name, rule, subject, template_body, flags, clears


# ── Draft generator ───────────────────────────────────────────────────────────
def generate_drafts(row, template_body, subject, ticket, agent_name):
    out_name  = str(row.get("Outgoing PA name",  "Outgoing Admin"))
    out_email = str(row.get("Outgoing PA email", ""))
    in_name   = str(row.get("Incoming PA name",  "Incoming Admin"))
    in_email  = str(row.get("Incoming PA email", ""))
    org       = str(row.get("Org name", "your organization"))

    body = template_body.replace("XXXXXX", agent_name).replace("PBS-XXXX", ticket)

    email = (
        f"Dear {out_name} ({out_email}) and {in_name} ({in_email}),\n\n"
        f"{body}\n\n"
        f"Best regards,\n{agent_name}\nWalmart Business Customer Care\nTicket: {ticket}"
    )
    sms = (
        f"Hi {out_name} & {in_name} — This is {agent_name} from Walmart Business Care. "
        f"We received your Primary Admin transfer request for {org} (Ticket: {ticket}). "
        f"Please check your email for next steps and respond within 2 business days."
    )
    vm = (
        f"Hello, this is an automated message from Walmart Business Customer Care. "
        f"We are calling regarding the Primary Admin transfer request for {org}, "
        f"ticket number {ticket}. "
        f"Please check your email from Walmart Business Care for next steps and "
        f"respond within two business days. Thank you and have a great day."
    )
    return email, sms, vm, subject


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    try:
        st.image(
            "https://corporate.walmart.com/content/dam/corporate/en/brand-guidelines/logo/Walmart-Logo.png",
            width=160
        )
    except Exception:
        st.markdown("### 🔄 PA Orchestrator")

    st.title("PA Transfer Orchestrator")

    # Google Sheet status
    gs_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    st.markdown(
        f'<span class="gs-badge">📊 Google Sheet Connected</span><br>'
        f'<small><a href="{gs_url}" target="_blank">Open in Google Sheets ↗</a></small>',
        unsafe_allow_html=True
    )
    if st.button("🔄 Refresh Data from Sheet"):
        clear_sheet_cache()
        st.rerun()

    st.divider()
    st.subheader("👤 Agent Info")
    agent_name = st.text_input("Your Name", value="Jessica R")

    st.divider()
    st.subheader("🔑 API Keys")
    with st.expander("Configure credentials", expanded=False):
        twilio_sid  = st.text_input("Twilio SID",      value="ACc774620f3504ddb56e88714ba4363604", type="password")
        twilio_auth = st.text_input("Twilio Auth",     value="", type="password")
        twilio_num  = st.text_input("Twilio Number",   value="+18773138895")
        twilio_wa   = st.text_input("WhatsApp From",   value="+14155238886")
        sg_key      = st.text_input("SendGrid Key",    value="", type="password")
        sg_sender   = st.text_input("Verified Sender", value="shridhar.reddy@gmail.com")

    st.divider()
    if APIS_AVAILABLE and twilio_auth and sg_key:
        live_mode = st.toggle("🔴 Live API Execution", value=False)
        if live_mode:
            st.warning("⚠️ Live mode — communications WILL be sent!")
    else:
        live_mode = False
        st.info("🟡 Simulation Mode\nFill API keys above to enable live sending.")

    # Write-back status
    st.divider()
    gc_check = get_gspread_client()
    if gc_check:
        st.success("✅ Sheet write-back enabled")
    else:
        st.warning(
            "⚠️ Sheet write-back not configured.\n\n"
            "Add `gcp_service_account` to Streamlit secrets to enable auto-logging to Google Sheet.\n\n"
            "Logs will be available for download instead."
        )

    st.divider()
    st.caption("v2.0 · Walmart Business CC")


# ── Session state init ────────────────────────────────────────────────────────
for key, default in {
    "stage": "select",
    "selected_org": None,
    "rule": None,
    "template_name": None,
    "email_draft": "",
    "sms_draft": "",
    "vm_draft": "",
    "email_subject": "",
    "ticket": None,
    "session_log": [],         # in-memory log for this session
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ── Load live data ────────────────────────────────────────────────────────────
try:
    db, templates, history_df, sim_df = load_all_sheets()
    DATA_OK = True
except Exception as e:
    DATA_OK = False
    st.error(
        f"**Could not load Google Sheet.** Make sure the sheet is shared as "
        f"'Anyone with the link can view'.\n\nError: {e}"
    )
    st.info(
        f"**Sheet to share:** [Open here]({gs_url})\n\n"
        "Go to Share → Change → Anyone with the link → Viewer → Done"
    )
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: SELECT RECORD
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state.stage == "select":
    st.markdown('<div class="stage-header">🔎 Select Organization</div>', unsafe_allow_html=True)

    col_m, col_total = st.columns([5, 1])
    with col_total:
        st.metric("Records", len(db))

    db["_has_flag"] = (
        (db["Org_PBI_Status"].astype(str).str.lower() == "yes") |
        (db["Outgoing PA Tax Exempt"].astype(str).str.lower() == "yes") |
        (db["Incoming PA existing account"].astype(str).str.lower() != "yes")
    )
    filter_opt = st.radio("Filter", ["All", "🚩 Flagged", "✅ Clear"], horizontal=True)
    if filter_opt == "🚩 Flagged":
        orgs = db[db["_has_flag"]]["Org name"].tolist()
    elif filter_opt == "✅ Clear":
        orgs = db[~db["_has_flag"]]["Org name"].tolist()
    else:
        orgs = db["Org name"].tolist()

    selected = st.selectbox("Select an organization to process", orgs)

    if selected:
        row = db[db["Org name"] == selected].iloc[0]
        preview_cols = ["Org name", "CID", "Outgoing PA name", "Outgoing PA email",
                        "Incoming PA name", "Incoming PA email", "Org_PBI_Status",
                        "Outgoing PA Tax Exempt", "Incoming PA existing account"]
        st.dataframe(pd.DataFrame([row[preview_cols]]), use_container_width=True, hide_index=True)

        if st.button("▶ Run Triage", type="primary"):
            st.session_state.selected_org = selected
            st.session_state.ticket = f"PBS-{datetime.now().strftime('%m%d%H%M%S')}"
            st.session_state.stage = "triage"
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: TRIAGE + PROOF OF WORK
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.stage == "triage":
    org    = st.session_state.selected_org
    ticket = st.session_state.ticket
    row    = db[db["Org name"] == org].iloc[0]

    template_name, rule, subject, template_body, flags, clears = run_rules_engine(row, templates)

    st.markdown(f'<div class="stage-header">🔍 Triage: {org}</div>', unsafe_allow_html=True)
    st.caption(f"Ticket: **{ticket}**")
    st.markdown(f'<span class="rule-badge">📋 {template_name}</span>', unsafe_allow_html=True)
    st.markdown(f"*{rule}*")
    st.divider()

    col_proof, col_flags = st.columns([3, 2])

    with col_proof:
        st.markdown("**📊 Full Record — Proof of Work**")
        proof_data = {
            "Org name": row.get("Org name"),
            "CID": row.get("CID"),
            "Outgoing PA": f"{row.get('Outgoing PA name')} ({row.get('Outgoing PA email')})",
            "Outgoing PA Phone": row.get("Outgoing PA phone"),
            "Tax Exempt": row.get("Outgoing PA Tax Exempt"),
            "Physical Card": row.get("Outgoing PA physical card"),
            "Incoming PA": f"{row.get('Incoming PA name')} ({row.get('Incoming PA email')})",
            "Incoming PA Phone": row.get("Incoming PA phone"),
            "Incoming PA Existing Account": row.get("Incoming PA existing account"),
            "Incoming PA Physical Card": row.get("Incoming PA Physical Card"),
            "PBI Status": row.get("Org_PBI_Status"),
            "Reason for Departure": row.get("Reason for departure"),
        }
        proof_df = pd.DataFrame(list(proof_data.items()), columns=["Field", "Value"])
        st.dataframe(proof_df, use_container_width=True, hide_index=True, height=400)

    with col_flags:
        st.markdown("**🚦 Flag Assessment**")
        for f in flags:
            st.markdown(f'<div class="flag-card">{f}</div>', unsafe_allow_html=True)
        for c in clears:
            st.markdown(f'<div class="clear-card">{c}</div>', unsafe_allow_html=True)
        st.divider()
        st.markdown(f"**📧 Template:** `{template_name}`")
        st.markdown(f"*Subject:* {subject}")

    st.divider()
    col_a, col_b, col_c = st.columns([2, 2, 4])
    with col_a:
        if st.button("✅ Confirm — Generate Drafts", type="primary"):
            email_d, sms_d, vm_d, subj = generate_drafts(
                row, template_body, subject, ticket, agent_name
            )
            st.session_state.update({
                "rule": rule,
                "template_name": template_name,
                "email_draft": email_d,
                "sms_draft": sms_d,
                "vm_draft": vm_d,
                "email_subject": subj,
                "stage": "draft",
            })
            st.rerun()
    with col_b:
        if st.button("⚠️ Override Template"):
            st.session_state.stage = "override"
            st.rerun()
    with col_c:
        if st.button("← Back to Record Selection"):
            st.session_state.stage = "select"
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: MANUAL OVERRIDE
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.stage == "override":
    org    = st.session_state.selected_org
    ticket = st.session_state.ticket
    row    = db[db["Org name"] == org].iloc[0]

    st.markdown('<div class="stage-header">⚠️ Manual Template Override</div>', unsafe_allow_html=True)
    chosen          = st.selectbox("Select template", templates["Template_Name"].tolist())
    override_reason = st.text_area("Reason for override (required)")

    if st.button("✅ Apply & Generate Drafts", type="primary") and override_reason.strip():
        trow  = templates[templates["Template_Name"] == chosen].iloc[0]
        email_d, sms_d, vm_d, subj = generate_drafts(
            row, trow["Body_Text"], trow["Default_Subject"], ticket, agent_name
        )
        st.session_state.update({
            "rule": f"OVERRIDE — {override_reason}",
            "template_name": chosen,
            "email_draft": email_d,
            "sms_draft": sms_d,
            "vm_draft": vm_d,
            "email_subject": subj,
            "stage": "draft",
        })
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: DRAFT REVIEW & EDIT
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.stage == "draft":
    org = st.session_state.selected_org
    row = db[db["Org name"] == org].iloc[0]

    st.markdown(f'<div class="stage-header">📝 Review & Edit Drafts — {org}</div>', unsafe_allow_html=True)
    st.caption(f"Ticket: **{st.session_state.ticket}** · Template: **{st.session_state.template_name}**")

    st.info(
        f"📤 **To:** {row.get('Outgoing PA name')} ({row.get('Outgoing PA email')}) "
        f"+ {row.get('Incoming PA name')} ({row.get('Incoming PA email')})  "
        f"📱 **Phone:** {row.get('Outgoing PA phone')} / {row.get('Incoming PA phone')}"
    )

    tab_email, tab_sms, tab_vm = st.tabs(["📧 Email", "💬 SMS / WhatsApp", "📞 Voicemail Script"])

    with tab_email:
        subj_edit  = st.text_input("Subject", value=st.session_state.email_subject)
        email_edit = st.text_area("Email Body", value=st.session_state.email_draft, height=320)
        st.session_state.email_draft   = email_edit
        st.session_state.email_subject = subj_edit

    with tab_sms:
        st.caption("160 chars = 1 SMS segment. WhatsApp has no limit.")
        sms_edit = st.text_area("SMS / WhatsApp Message", value=st.session_state.sms_draft, height=180)
        chars = len(sms_edit)
        color = "green" if chars <= 160 else "orange"
        st.markdown(f"<small style='color:{color}'>{chars} chars · {max(1,-(-chars//160))} segment(s)</small>", unsafe_allow_html=True)
        st.session_state.sms_draft = sms_edit

    with tab_vm:
        st.caption("This will be read aloud via Twilio Voice (Alice). Keep under ~75 words / 30 seconds.")
        vm_edit = st.text_area("Voicemail Script", value=st.session_state.vm_draft, height=180)
        wc = len(vm_edit.split())
        wcolor = "green" if wc <= 75 else "orange"
        st.markdown(f"<small style='color:{wcolor}'>{wc} words · ~{wc//3}s</small>", unsafe_allow_html=True)
        st.session_state.vm_draft = vm_edit

    st.divider()
    mode_label = "🔴 LIVE" if live_mode else "🟡 SIMULATION"
    st.markdown(f"**Execution mode: {mode_label}**")

    col1, col2, col3 = st.columns([3, 2, 3])
    with col1:
        if st.button(f"🚀 Approve & Send ({mode_label})", type="primary"):
            st.session_state.stage = "sending"
            st.rerun()
    with col2:
        if st.button("← Back to Triage"):
            st.session_state.stage = "triage"
            st.rerun()
    with col3:
        if st.button("🚫 Reject / Log Override"):
            st.session_state.stage = "reject"
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: SEND
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.stage == "sending":
    org    = st.session_state.selected_org
    row    = db[db["Org name"] == org].iloc[0]
    ticket = st.session_state.ticket

    st.markdown(f'<div class="stage-header">🚀 Sending — {org}</div>', unsafe_allow_html=True)

    out_email = str(row.get("Outgoing PA email", ""))
    raw_phone = str(row.get("Outgoing PA phone", "")).replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    recipient_phone = f"+1{raw_phone}" if not raw_phone.startswith("+") else raw_phone
    results = {}

    progress = st.progress(0, text="Starting...")

    # ── Email ──────────────────────────────────────────────────────────────────
    progress.progress(10, text="📧 Sending email...")
    if live_mode:
        try:
            sg   = SendGridAPIClient(sg_key)
            mail = Mail(
                from_email=sg_sender,
                to_emails=out_email,
                subject=st.session_state.email_subject,
                html_content=st.session_state.email_draft.replace("\n", "<br>")
            )
            resp = sg.send(mail)
            results["Email"] = f"✅ Sent to {out_email} (HTTP {resp.status_code})"
        except Exception as e:
            results["Email"] = f"❌ Failed: {e}"
    else:
        results["Email"] = f"🟡 Simulated → {out_email}"

    # ── SMS ────────────────────────────────────────────────────────────────────
    progress.progress(40, text="💬 Sending SMS...")
    if live_mode:
        try:
            tw  = TwilioClient(twilio_sid, twilio_auth)
            msg = tw.messages.create(
                from_=f"whatsapp:+{twilio_wa.lstrip('+')}",
                to=f"whatsapp:{recipient_phone}",
                body=st.session_state.sms_draft
            )
            results["SMS"] = f"✅ Sent to {recipient_phone} (SID: {msg.sid})"
        except Exception as e:
            results["SMS"] = f"❌ Failed: {e}"
    else:
        results["SMS"] = f"🟡 Simulated → {recipient_phone}"

    # ── Voice ──────────────────────────────────────────────────────────────────
    progress.progress(70, text="📞 Initiating voice call...")
    if live_mode:
        try:
            tw   = TwilioClient(twilio_sid, twilio_auth)
            twiml = f"<Response><Say voice='alice'>{st.session_state.vm_draft}</Say></Response>"
            call  = tw.calls.create(from_=twilio_num, to=recipient_phone, twiml=twiml)
            results["Voice"] = f"✅ Called {recipient_phone} (SID: {call.sid})"
        except Exception as e:
            results["Voice"] = f"❌ Failed: {e}"
    else:
        results["Voice"] = f"🟡 Simulated → {recipient_phone}"

    # ── Log to Google Sheet ────────────────────────────────────────────────────
    progress.progress(88, text="📝 Logging to Google Sheet...")
    ts     = datetime.now().isoformat()
    status = "LIVE" if live_mode else "SIMULATED"

    history_row = {
        "Timestamp":        ts,
        "Org_ID":           row.get("CID"),
        "Findings_Summary": f"{st.session_state.rule} | Ticket: {ticket}",
        "Draft_Email":      st.session_state.email_draft[:500],
        "Draft_SMS":        st.session_state.sms_draft,
        "Draft_Voicemail":  st.session_state.vm_draft,
    }
    sim_row = {
        "Timestamp":     ts,
        "Org_ID":        row.get("CID"),
        "Final_Outcome": st.session_state.template_name,
        "Action_Taken":  st.session_state.rule,
        "Sent_To":       f"{out_email} / {recipient_phone}",
    }

    wrote_history = append_to_sheet("Interaction_History", history_row)
    wrote_sim     = append_to_sheet("Simulation_Log",      sim_row)

    # Always store in session memory too
    st.session_state.session_log.append({**sim_row, "Status": status})

    progress.progress(100, text="Done!")
    st.success(f"✅ All channels processed for **{org}** · Ticket: **{ticket}**")

    # Results
    for ch, res in results.items():
        st.markdown(f"- **{ch}:** {res}")

    # Sheet write-back feedback
    if wrote_history and wrote_sim:
        st.success("📊 Logs written to Google Sheet automatically.")
    else:
        st.info(
            "📊 Logs not written to sheet (no service account configured). "
            "Download the session log below."
        )
        log_df = pd.DataFrame(st.session_state.session_log)
        csv    = log_df.to_csv(index=False).encode()
        st.download_button(
            "💾 Download Session Log (CSV)",
            data=csv,
            file_name=f"session_log_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv"
        )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("▶ Process Another Record", type="primary"):
            st.session_state.stage = "select"
            st.rerun()
    with col2:
        if st.button("🔄 Reload Data from Sheet"):
            clear_sheet_cache()
            st.session_state.stage = "select"
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STAGE: REJECT
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.stage == "reject":
    org    = st.session_state.selected_org
    ticket = st.session_state.ticket
    row    = db[db["Org name"] == org].iloc[0]

    st.markdown(f'<div class="stage-header">🚫 Reject — {org}</div>', unsafe_allow_html=True)
    reason = st.text_area("Reason for rejection (required)")

    if st.button("Submit Rejection Log", type="primary") and reason.strip():
        sim_row = {
            "Timestamp": datetime.now().isoformat(),
            "Org_ID": row.get("CID"),
            "Final_Outcome": "REJECTED",
            "Action_Taken": reason,
            "Sent_To": "N/A",
        }
        append_to_sheet("Simulation_Log", sim_row)
        st.session_state.session_log.append(sim_row)
        st.success("Rejection logged.")
        if st.button("← Back to Records"):
            st.session_state.stage = "select"
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# BOTTOM: SESSION AUDIT TRAIL
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state.session_log:
    st.divider()
    with st.expander(f"📋 Session Log ({len(st.session_state.session_log)} action(s))", expanded=False):
        st.dataframe(
            pd.DataFrame(st.session_state.session_log),
            use_container_width=True, hide_index=True
        )
