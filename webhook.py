"""
WhatsApp Bulk Sender — Webhook Server & Web Dashboard
FastAPI endpoints for WhatsApp delivery status callbacks, REST API,
and modern responsive Web Dashboard for Vercel and production deployment.
"""

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import database
import csv_handler

logger = logging.getLogger(__name__)

# Valid delivery statuses (anything else is rejected)
_VALID_STATUSES = {"queued", "sent", "delivered", "read", "failed"}

# -- Lifespan -------------------------------------------------

_initialized = False


def mark_initialized():
    """Called by main.py to prevent double-init in telegram mode."""
    global _initialized
    _initialized = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _initialized
    if not _initialized:
        await database.init()
        _initialized = True
        logger.info(f"{config.BOT_NAME} v{config.BOT_VERSION} webhook server started")
    yield


app = FastAPI(title=config.BOT_NAME, version=config.BOT_VERSION, lifespan=lifespan)

# Enable CORS for webhooks and frontend interactions
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Pydantic Models -------------------------------------------

class SingleMessageRequest(BaseModel):
    phone: str
    message: str


class BulkSendRequest(BaseModel):
    campaign_id: int
    message: str
    dry_run: bool = False


class TemplateRequest(BaseModel):
    name: str
    category: str = "general"
    body: str


# -- Meta Cloud API Webhook ------------------------------------

@app.get("/webhook")
async def meta_verify(request: Request):
    """Meta webhook verification (challenge-response)."""
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and config.WA_VERIFY_TOKEN and hmac.compare_digest(token, config.WA_VERIFY_TOKEN):
        logger.info("Meta webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def meta_webhook(request: Request):
    """Receive Meta Cloud API delivery status updates."""
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse webhook JSON: {e}")
        return {"status": "error"}

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Status updates (delivery receipts)
            for status_update in value.get("statuses", []):
                sid = status_update.get("id", "")
                new_status = status_update.get("status", "")
                if sid and new_status in _VALID_STATUSES:
                    await database.update_message_status(sid, new_status)
                    logger.debug(f"Status update: {sid} -> {new_status}")
                elif new_status:
                    logger.warning(f"Unknown status '{new_status}' for {sid}, ignoring")

    return {"status": "ok"}


# -- Twilio Status Callback ------------------------------------

@app.post("/twilio-status")
async def twilio_status(request: Request):
    """Handle Twilio delivery status callbacks."""
    form = await request.form()
    sid = str(form.get("MessageSid", ""))
    status = str(form.get("MessageStatus", ""))

    status_map = {
        "queued": "queued",
        "sent": "sent",
        "delivered": "delivered",
        "read": "read",
        "undelivered": "failed",
        "failed": "failed",
    }
    normalized = status_map.get(status)

    if sid and normalized:
        await database.update_message_status(sid, normalized)

    return {"status": "ok"}


# -- Health & System Info --------------------------------------

@app.get("/health")
async def health():
    warnings = config.validate()
    return {
        "status": "ok",
        "bot": config.BOT_NAME,
        "version": config.BOT_VERSION,
        "provider": config.WA_PROVIDER,
        "configured": len(warnings) == 0,
        "warnings": warnings,
    }


# -- REST API Endpoints ----------------------------------------

@app.get("/api/status")
async def api_status():
    warnings = config.validate()
    return {
        "bot": config.BOT_NAME,
        "version": config.BOT_VERSION,
        "provider": config.WA_PROVIDER,
        "configured": len(warnings) == 0,
        "warnings": warnings,
        "database": config.DB_PATH,
        "requires_auth": bool(config.DASHBOARD_PASSWORD),
    }


@app.get("/api/stats")
async def api_stats():
    contact_count = await database.get_contact_count()
    campaigns = await database.get_all_campaigns()
    stats = await database.get_send_stats()

    total_out = sum(stats.get(s, 0) for s in ("sent", "delivered", "read", "failed"))
    delivered = stats.get("delivered", 0) + stats.get("read", 0)
    rate = (delivered / total_out * 100) if total_out > 0 else 0.0

    return {
        "contacts": contact_count,
        "campaigns": len(campaigns),
        "sent": stats.get("sent", 0) + stats.get("delivered", 0) + stats.get("read", 0),
        "delivered": stats.get("delivered", 0),
        "read": stats.get("read", 0),
        "failed": stats.get("failed", 0),
        "queued": stats.get("queued", 0),
        "total": stats.get("total", 0),
        "delivery_rate": round(rate, 1),
    }


@app.get("/api/contacts")
async def api_contacts(limit: int = 200):
    contacts = await database.get_all_contacts()
    return contacts[:limit]


@app.post("/api/contacts/import")
async def api_import_contacts(
    file: UploadFile = File(...),
    campaign_name: str = Form(""),
):
    content = await file.read()
    contacts, errors = csv_handler.parse_csv(content)

    if not contacts:
        return JSONResponse(
            status_code=400,
            content={"error": "No valid contacts found in CSV", "details": errors},
        )

    contact_ids = []
    for c in contacts:
        cid = await database.upsert_contact(
            first_name=c["first_name"],
            last_name=c["last_name"],
            phone=c["phone"],
            email=c.get("email", ""),
        )
        contact_ids.append(cid)

    campaign_id = None
    if campaign_name.strip():
        campaign_id = await database.create_campaign(name=campaign_name.strip(), message="")
        await database.add_contacts_to_campaign(campaign_id, contact_ids)

    return {
        "status": "ok",
        "imported": len(contacts),
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "warnings": errors[:15],
    }


@app.get("/api/campaigns")
async def api_campaigns():
    return await database.get_all_campaigns()


@app.get("/api/campaigns/{campaign_id}/contacts")
async def api_campaign_contacts(campaign_id: int):
    return await database.get_contacts_for_campaign(campaign_id)


@app.get("/api/templates")
async def api_templates():
    return await database.get_all_templates()


@app.post("/api/templates")
async def api_create_template(tpl: TemplateRequest):
    if not tpl.name.strip() or not tpl.body.strip():
        raise HTTPException(status_code=400, detail="Name and body are required")
    await database.create_template(tpl.name.strip(), tpl.category.strip(), tpl.body)
    return {"status": "ok", "name": tpl.name}


@app.delete("/api/templates/{name}")
async def api_delete_template(name: str):
    await database.delete_template(name)
    return {"status": "ok", "deleted": name}


@app.get("/api/messages")
async def api_messages(limit: int = 50):
    return await database.get_recent_messages(limit)


@app.post("/api/send")
async def api_send_single(req: SingleMessageRequest):
    if not req.phone.strip() or not req.message.strip():
        raise HTTPException(status_code=400, detail="Phone and message are required")

    from whatsapp import WhatsAppClient

    client = WhatsAppClient()
    result = await client.send_message(req.phone.strip(), req.message)

    await database.log_message(
        phone=req.phone.strip(),
        content=req.message[:500],
        status=result.get("status", "failed"),
        wa_message_id=result.get("sid", ""),
        error_message=result.get("error", ""),
    )

    return result


@app.post("/api/send-bulk")
async def api_send_bulk(req: BulkSendRequest):
    campaign = await database.get_campaign(req.campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    contacts = await database.get_contacts_for_campaign(req.campaign_id)
    if not contacts:
        raise HTTPException(status_code=400, detail="No contacts found in this campaign")

    if req.dry_run:
        return {
            "status": "dry_run",
            "total": len(contacts),
            "message": f"Dry-run mode: {len(contacts)} recipients verified. No messages dispatched.",
        }

    await database.update_campaign_message(req.campaign_id, req.message)
    await database.update_campaign_status(req.campaign_id, "sending")

    from whatsapp import WhatsAppClient, _render_message

    client = WhatsAppClient()
    sent = 0
    failed = 0
    results = []

    for c in contacts:
        body = _render_message(req.message, {
            "first_name": c.get("first_name", "Contact"),
            "last_name": c.get("last_name", ""),
            "phone": c.get("phone", ""),
            "name": f"{c.get('first_name', '')} {c.get('last_name', '')}".strip(),
        })

        res = await client.send_message(c["phone"], body)
        await database.log_message(
            phone=c["phone"],
            content=body[:500],
            status=res.get("status", "failed"),
            campaign_id=req.campaign_id,
            contact_id=c.get("id"),
            wa_message_id=res.get("sid", ""),
            error_message=res.get("error", ""),
        )

        if res.get("status") == "sent":
            sent += 1
        else:
            failed += 1

        results.append({
            "phone": c["phone"],
            "status": res.get("status", "failed"),
            "error": res.get("error", ""),
        })

    await database.update_campaign_status(req.campaign_id, "sent")

    return {
        "status": "completed",
        "total": len(contacts),
        "sent": sent,
        "failed": failed,
        "results": results[:50],
    }


# -- Web Dashboard UI (Pure Native HTML+CSS+JS, Zero Dependencies) --

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WhatsApp Bulk Sender</title>
  <style>
    :root {
      --bg: #090d16;
      --card: #111827;
      --card-border: #1f2937;
      --input-bg: #1e293b;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --primary: #10b981;
      --primary-hover: #059669;
      --danger: #ef4444;
      --warning: #f59e0b;
      --blue: #3b82f6;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--font);
      line-height: 1.5;
      padding-bottom: 50px;
    }
    header {
      background: var(--card);
      border-bottom: 1px solid var(--card-border);
      padding: 16px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand h1 { font-size: 1.25rem; font-weight: 700; color: #fff; }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .badge-green { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge-yellow { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-blue { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4); }
    .badge-red { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
    .container { max-width: 1200px; margin: 0 auto; padding: 24px 16px; }
    
    /* Config Banner */
    .banner {
      background: rgba(245, 158, 11, 0.1);
      border: 1px solid rgba(245, 158, 11, 0.3);
      border-radius: 8px;
      padding: 14px 18px;
      margin-bottom: 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 0.9rem;
    }
    .banner-text { color: #fcd34d; }
    .banner a { color: #fff; text-decoration: underline; font-weight: 600; }

    /* Stats Grid */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }
    .stat-card {
      background: var(--card);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 18px 20px;
    }
    .stat-title { font-size: 0.82rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600; }
    .stat-val { font-size: 1.85rem; font-weight: 700; margin-top: 4px; color: #fff; }

    /* Navigation Tabs */
    .tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 24px;
      overflow-x: auto;
      padding-bottom: 4px;
    }
    .tab-btn {
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 10px 18px;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      border-bottom: 2px solid transparent;
      transition: all 0.2s ease;
      white-space: nowrap;
    }
    .tab-btn:hover { color: var(--text); }
    .tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }

    /* Cards & Forms */
    .card {
      background: var(--card);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 24px;
    }
    .card-title { font-size: 1.15rem; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; justify-content: space-between; }
    .form-group { margin-bottom: 18px; }
    label { display: block; font-size: 0.88rem; font-weight: 600; margin-bottom: 6px; color: var(--text); }
    input[type="text"], input[type="password"], select, textarea {
      width: 100%;
      background: var(--input-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 8px;
      font-family: inherit;
      font-size: 0.92rem;
    }
    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.2);
    }
    .btn {
      background: var(--primary);
      color: #fff;
      border: none;
      padding: 10px 20px;
      border-radius: 8px;
      font-size: 0.92rem;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      transition: background 0.15s ease;
    }
    .btn:hover { background: var(--primary-hover); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-secondary { background: #374151; color: #fff; }
    .btn-secondary:hover { background: #4b5563; }
    .btn-danger { background: var(--danger); }
    .btn-danger:hover { background: #dc2626; }
    .btn-sm { padding: 5px 10px; font-size: 0.8rem; border-radius: 6px; }

    /* Tags */
    .tag-btn {
      background: #1f2937;
      color: #93c5fd;
      border: 1px solid #374151;
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 0.78rem;
      cursor: pointer;
      margin-right: 6px;
      margin-top: 4px;
    }
    .tag-btn:hover { background: #374151; }

    /* Tables */
    .table-responsive { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem; }
    th { padding: 12px 14px; border-bottom: 1px solid var(--card-border); color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 0.75rem; }
    td { padding: 12px 14px; border-bottom: 1px solid var(--card-border); }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }

    /* Code & Preview Box */
    .preview-box {
      background: #0b0f19;
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 14px;
      font-size: 0.9rem;
      color: #38bdf8;
      white-space: pre-wrap;
      min-height: 50px;
    }
    pre code {
      background: #0b0f19;
      display: block;
      padding: 12px;
      border-radius: 6px;
      border: 1px solid var(--card-border);
      font-family: monospace;
      font-size: 0.85rem;
      color: #a7f3d0;
      overflow-x: auto;
    }
    .notice { font-size: 0.82rem; color: var(--text-muted); margin-top: 6px; }
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* Drag & Drop Upload Zone */
    .upload-zone {
      border: 2px dashed var(--card-border);
      border-radius: 10px;
      padding: 36px 20px;
      text-align: center;
      cursor: pointer;
      transition: all 0.2s ease;
      background: rgba(255, 255, 255, 0.01);
    }
    .upload-zone:hover { border-color: var(--primary); background: rgba(16, 185, 129, 0.03); }
    .status-msg { margin-top: 14px; padding: 12px; border-radius: 8px; font-size: 0.9rem; display: none; }
    .status-msg.show { display: block; }
    .status-success { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .status-error { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <span style="font-size: 1.5rem;">📱</span>
      <div>
        <h1>WhatsApp Bulk Sender</h1>
        <div style="font-size: 0.78rem; color: var(--text-muted);">
          v1.0.0 &bull; Provider: <span id="hdrProvider" class="badge badge-blue">TWILIO</span>
        </div>
      </div>
    </div>
    <div style="display: flex; gap: 8px; align-items: center;">
      <span id="hdrStatusBadge" class="badge badge-yellow">Checking...</span>
      <a href="/docs" target="_blank" class="btn btn-secondary btn-sm">API Docs</a>
      <a href="https://github.com/SoCloseSociety/WhatsappSender" target="_blank" class="btn btn-secondary btn-sm">GitHub</a>
    </div>
  </header>

  <div class="container">

    <!-- Config Warning Banner -->
    <div id="configBanner" class="banner" style="display: none;">
      <div class="banner-text">
        ⚠️ <strong>WhatsApp Credentials Missing:</strong> Add your credentials in your <strong>Vercel Project Settings &rarr; Environment Variables</strong>.
        <span id="bannerDetails"></span>
      </div>
      <button class="btn btn-sm btn-secondary" onclick="switchTab('tabSetup')">View Setup Guide</button>
    </div>

    <!-- Stats Grid -->
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-title">Total Contacts</div>
        <div class="stat-val" id="statContacts">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-title">Campaigns</div>
        <div class="stat-val" id="statCampaigns">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-title">Messages Sent</div>
        <div class="stat-val" id="statSent" style="color: #34d399;">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-title">Delivery Rate</div>
        <div class="stat-val" id="statRate">0%</div>
      </div>
    </div>

    <!-- Navigation Tabs -->
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('tabSend')">📢 Send Campaign</button>
      <button class="tab-btn" onclick="switchTab('tabImport')">📥 Import CSV</button>
      <button class="tab-btn" onclick="switchTab('tabContacts')">👥 Contacts</button>
      <button class="tab-btn" onclick="switchTab('tabMessages')">📊 Message Logs</button>
      <button class="tab-btn" onclick="switchTab('tabTemplates')">📋 Templates</button>
      <button class="tab-btn" onclick="switchTab('tabTest')">💬 Quick Test</button>
      <button class="tab-btn" onclick="switchTab('tabSetup')">⚙️ Webhook & Vercel Setup</button>
    </div>

    <!-- TAB 1: SEND CAMPAIGN -->
    <div id="tabSend" class="tab-content active">
      <div class="card">
        <div class="card-title">📢 Send Bulk Campaign</div>
        
        <div class="form-group">
          <label>Select Target Campaign</label>
          <select id="sendCampaignSelect" onchange="onCampaignChange()">
            <option value="">-- Choose a Campaign --</option>
          </select>
          <div id="campaignRecipientNotice" class="notice"></div>
        </div>

        <div class="form-group">
          <label>Select Template (Optional)</label>
          <select id="sendTemplateSelect" onchange="applyTemplateToMessage()">
            <option value="">-- Write Custom Message --</option>
          </select>
        </div>

        <div class="form-group">
          <label>Message Content</label>
          <div style="margin-bottom: 6px;">
            <span style="font-size: 0.78rem; color: var(--text-muted); margin-right: 6px;">Insert Tag:</span>
            <button type="button" class="tag-btn" onclick="insertTag('{first_name}')">{first_name}</button>
            <button type="button" class="tag-btn" onclick="insertTag('{last_name}')">{last_name}</button>
            <button type="button" class="tag-btn" onclick="insertTag('{name}')">{name}</button>
            <button type="button" class="tag-btn" onclick="insertTag('{phone}')">{phone}</button>
          </div>
          <textarea id="sendMessageText" rows="5" placeholder="Bonjour {first_name} ! Voici votre mise à jour..." oninput="updateMessagePreview()"></textarea>
        </div>

        <div class="form-group">
          <label>Live Preview</label>
          <div id="messagePreviewBox" class="preview-box">(Type a message above to see preview)</div>
        </div>

        <div class="form-group" style="display: flex; align-items: center; gap: 8px;">
          <input type="checkbox" id="sendDryRun" checked>
          <label for="sendDryRun" style="margin-bottom: 0; cursor: pointer;">Dry-Run Mode (Test without actually sending messages)</label>
        </div>

        <button id="btnSendBulk" class="btn" onclick="executeSendBulk()">
          <span>📤 Dispatch Campaign</span>
        </button>

        <div id="sendBulkStatus" class="status-msg"></div>
      </div>
    </div>

    <!-- TAB 2: IMPORT CSV -->
    <div id="tabImport" class="tab-content">
      <div class="card">
        <div class="card-title">📥 Import Contacts via CSV</div>
        
        <div class="form-group">
          <label>Campaign Name</label>
          <input type="text" id="importCampaignName" placeholder="e.g. VIP Customers Oct 2026">
        </div>

        <div class="form-group">
          <label>Select CSV File</label>
          <div class="upload-zone" onclick="document.getElementById('csvFileInput').click()">
            <span style="font-size: 2rem;">📄</span>
            <div style="font-weight: 600; margin-top: 8px;" id="csvFileLabel">Click or Drag & Drop CSV here</div>
            <div class="notice">Supports single-column numbers, Shopify exports, Google Contacts, and custom CSVs</div>
          </div>
          <input type="file" id="csvFileInput" accept=".csv" style="display: none;" onchange="onCSVSelected(event)">
        </div>

        <button id="btnImport" class="btn" onclick="executeImport()" disabled>
          <span>📥 Import Contacts</span>
        </button>

        <div id="importStatus" class="status-msg"></div>
      </div>
    </div>

    <!-- TAB 3: CONTACTS -->
    <div id="tabContacts" class="tab-content">
      <div class="card">
        <div class="card-title">
          <span>👥 Imported Contacts (<span id="contactsCountBadge">0</span>)</span>
          <button class="btn btn-secondary btn-sm" onclick="loadContacts()">🔄 Refresh</button>
        </div>
        <div class="table-responsive">
          <table>
            <thead>
              <tr>
                <th>Phone</th>
                <th>First Name</th>
                <th>Last Name</th>
                <th>Email</th>
                <th>Added</th>
              </tr>
            </thead>
            <tbody id="contactsTableBody">
              <tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No contacts imported yet.</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 4: MESSAGE LOGS -->
    <div id="tabMessages" class="tab-content">
      <div class="card">
        <div class="card-title">
          <span>📊 Recent Deliveries</span>
          <button class="btn btn-secondary btn-sm" onclick="loadMessages()">🔄 Refresh</button>
        </div>
        <div class="table-responsive">
          <table>
            <thead>
              <tr>
                <th>Phone</th>
                <th>Status</th>
                <th>Message Content</th>
                <th>Timestamp</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody id="messagesTableBody">
              <tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No messages logged yet.</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TAB 5: TEMPLATES -->
    <div id="tabTemplates" class="tab-content">
      <div class="card">
        <div class="card-title">➕ Create Template</div>
        <div class="form-group">
          <label>Template Name</label>
          <input type="text" id="tplName" placeholder="e.g. welcome_offer">
        </div>
        <div class="form-group">
          <label>Category</label>
          <select id="tplCategory">
            <option value="general">general</option>
            <option value="broadcast">broadcast</option>
            <option value="reminder">reminder</option>
          </select>
        </div>
        <div class="form-group">
          <label>Content</label>
          <textarea id="tplBody" rows="3" placeholder="Bonjour {first_name}..."></textarea>
        </div>
        <button class="btn" onclick="saveTemplate()">Save Template</button>
        <div id="tplStatus" class="status-msg"></div>
      </div>

      <div class="card">
        <div class="card-title">Saved Templates</div>
        <div id="templatesList">Loading...</div>
      </div>
    </div>

    <!-- TAB 6: QUICK TEST -->
    <div id="tabTest" class="tab-content">
      <div class="card">
        <div class="card-title">💬 Test WhatsApp Send</div>
        <div class="form-group">
          <label>Recipient Phone Number (E.164 with country code)</label>
          <input type="text" id="testPhone" placeholder="+33612345678">
        </div>
        <div class="form-group">
          <label>Test Message</label>
          <textarea id="testMessage" rows="3" placeholder="Hello! Test message from WhatsApp Bulk Sender."></textarea>
        </div>
        <button class="btn" onclick="executeTestSend()">Send Test Message</button>
        <div id="testStatus" class="status-msg"></div>
      </div>
    </div>

    <!-- TAB 7: SETUP & VERCEL GUIDE -->
    <div id="tabSetup" class="tab-content">
      <div class="card">
        <div class="card-title">⚙️ Webhook Endpoints (Copy & Paste to Meta / Twilio)</div>
        <p class="notice" style="margin-bottom: 16px;">
          Configure these callback URLs in your WhatsApp provider settings so delivery receipts (sent, delivered, read) sync automatically.
        </p>

        <div class="form-group">
          <label>Meta Cloud API Webhook URL</label>
          <pre><code id="codeMetaUrl">https://.../webhook</code></pre>
          <div class="notice">In Meta App Dashboard &rarr; WhatsApp &rarr; Configuration &rarr; Callback URL</div>
        </div>

        <div class="form-group">
          <label>Twilio Status Callback URL</label>
          <pre><code id="codeTwilioUrl">https://.../twilio-status</code></pre>
          <div class="notice">In Twilio Console &rarr; Messaging &rarr; WhatsApp Sandbox &rarr; Status callback URL</div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">🚀 Vercel Environment Variables Guide</div>
        <p class="notice" style="margin-bottom: 12px;">
          Add these keys in your <strong>Vercel Project &rarr; Settings &rarr; Environment Variables</strong>:
        </p>
        <pre><code># Provider ("twilio" or "meta")
WA_PROVIDER=twilio

# For Twilio:
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886

# OR for Meta Cloud API:
WA_PROVIDER=meta
WA_PHONE_NUMBER_ID=...
WA_ACCESS_TOKEN=...
WA_VERIFY_TOKEN=my_secret_verify_token

# Optional Dashboard Protection:
DASHBOARD_PASSWORD=secret</code></pre>
      </div>
    </div>

  </div>

  <script>
    // State
    let templatesData = [];
    let campaignsData = [];
    let contactsData = [];

    // Switch Tabs
    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      const activeBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.getAttribute('onclick').includes(tabId));
      if (activeBtn) activeBtn.classList.add('active');
      const target = document.getElementById(tabId);
      if (target) target.classList.add('active');
    }

    // Insert Tag in Message
    function insertTag(tag) {
      const textarea = document.getElementById('sendMessageText');
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const text = textarea.value;
      textarea.value = text.substring(0, start) + tag + text.substring(end);
      textarea.focus();
      textarea.selectionStart = textarea.selectionEnd = start + tag.length;
      updateMessagePreview();
    }

    // Live Message Preview
    function updateMessagePreview() {
      const msg = document.getElementById('sendMessageText').value;
      const sample = (contactsData && contactsData.length > 0) ? contactsData[0] : { first_name: 'Jean', last_name: 'Dupont', phone: '+33612345678' };
      let rendered = msg
        .replace(/{first_name}/g, sample.first_name || 'Contact')
        .replace(/{last_name}/g, sample.last_name || '')
        .replace(/{name}/g, ((sample.first_name || '') + ' ' + (sample.last_name || '')).trim() || 'Contact')
        .replace(/{phone}/g, sample.phone || '+33612345678');
      document.getElementById('messagePreviewBox').innerText = rendered || '(Type a message above to see preview)';
    }

    // Apply selected template
    function applyTemplateToMessage() {
      const select = document.getElementById('sendTemplateSelect');
      const tpl = templatesData.find(t => t.name === select.value);
      if (tpl) {
        document.getElementById('sendMessageText').value = tpl.body;
        updateMessagePreview();
      }
    }

    // Campaign dropdown change
    function onCampaignChange() {
      const sel = document.getElementById('sendCampaignSelect');
      const camp = campaignsData.find(c => c.id == sel.value);
      const notice = document.getElementById('campaignRecipientNotice');
      if (camp) {
        notice.innerText = `Campaign contains ${camp.contact_count || 0} recipient(s).`;
      } else {
        notice.innerText = '';
      }
    }

    // File selected handler
    let selectedFile = null;
    function onCSVSelected(event) {
      const file = event.target.files[0];
      if (file) {
        selectedFile = file;
        document.getElementById('csvFileLabel').innerText = `Selected: ${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
        document.getElementById('btnImport').disabled = false;
        if (!document.getElementById('importCampaignName').value) {
          document.getElementById('importCampaignName').value = file.name.replace(/\\.csv$/i, '');
        }
      }
    }

    // Execute CSV Import
    async function executeImport() {
      if (!selectedFile) return;
      const btn = document.getElementById('btnImport');
      const status = document.getElementById('importStatus');
      btn.disabled = true;
      status.className = 'status-msg';
      status.innerText = 'Importing and validating contacts...';
      status.classList.add('show');

      const formData = new FormData();
      formData.append('file', selectedFile);
      formData.append('campaign_name', document.getElementById('importCampaignName').value);

      try {
        const res = await fetch('/api/contacts/import', { method: 'POST', body: formData });
        const data = await res.json();
        if (res.ok) {
          status.className = 'status-msg status-success show';
          status.innerText = `✅ Successfully imported ${data.imported} contacts into campaign "${data.campaign_name || 'Import'}"!`;
          loadStats();
          loadCampaigns();
          loadContacts();
        } else {
          status.className = 'status-msg status-error show';
          status.innerText = `❌ Import error: ${data.error || 'Failed to parse CSV'}`;
        }
      } catch (err) {
        status.className = 'status-msg status-error show';
        status.innerText = `❌ Error: ${err.message}`;
      } finally {
        btn.disabled = false;
      }
    }

    // Send Bulk Campaign
    async function executeSendBulk() {
      const campaignId = document.getElementById('sendCampaignSelect').value;
      const message = document.getElementById('sendMessageText').value;
      const dryRun = document.getElementById('sendDryRun').checked;
      const btn = document.getElementById('btnSendBulk');
      const status = document.getElementById('sendBulkStatus');

      if (!campaignId) {
        alert('Please select a target campaign.');
        return;
      }
      if (!message.trim()) {
        alert('Please write a message.');
        return;
      }

      btn.disabled = true;
      status.className = 'status-msg';
      status.innerText = dryRun ? 'Verifying dry-run...' : 'Dispatching WhatsApp messages...';
      status.classList.add('show');

      try {
        const res = await fetch('/api/send-bulk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ campaign_id: parseInt(campaignId), message: message, dry_run: dryRun })
        });
        const data = await res.json();
        if (res.ok) {
          status.className = 'status-msg status-success show';
          if (data.status === 'dry_run') {
            status.innerText = `🧪 DRY RUN OK: ${data.total} recipients verified. No charges incurred.`;
          } else {
            status.innerText = `✅ Completed! Dispatched: ${data.sent} sent | ${data.failed} failed.`;
          }
          loadStats();
          loadMessages();
        } else {
          status.className = 'status-msg status-error show';
          status.innerText = `❌ Failed: ${data.detail || data.error || 'Execution failed'}`;
        }
      } catch (err) {
        status.className = 'status-msg status-error show';
        status.innerText = `❌ Error: ${err.message}`;
      } finally {
        btn.disabled = false;
      }
    }

    // Quick Test Send
    async function executeTestSend() {
      const phone = document.getElementById('testPhone').value;
      const message = document.getElementById('testMessage').value;
      const status = document.getElementById('testStatus');

      if (!phone || !message) {
        alert('Please provide phone number and message.');
        return;
      }

      status.className = 'status-msg';
      status.innerText = 'Sending test WhatsApp message...';
      status.classList.add('show');

      try {
        const res = await fetch('/api/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone, message })
        });
        const data = await res.json();
        if (data.status === 'sent') {
          status.className = 'status-msg status-success show';
          status.innerText = `✅ Message sent successfully! SID: ${data.sid || 'ok'}`;
          loadStats();
          loadMessages();
        } else {
          status.className = 'status-msg status-error show';
          status.innerText = `❌ Send failed: ${data.error || 'Unknown error'}`;
        }
      } catch (err) {
        status.className = 'status-msg status-error show';
        status.innerText = `❌ Error: ${err.message}`;
      }
    }

    // Save Template
    async function saveTemplate() {
      const name = document.getElementById('tplName').value;
      const category = document.getElementById('tplCategory').value;
      const body = document.getElementById('tplBody').value;
      const status = document.getElementById('tplStatus');

      if (!name || !body) {
        alert('Please provide name and body.');
        return;
      }

      try {
        const res = await fetch('/api/templates', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, category, body })
        });
        if (res.ok) {
          status.className = 'status-msg status-success show';
          status.innerText = `✅ Template "${name}" saved!`;
          document.getElementById('tplName').value = '';
          document.getElementById('tplBody').value = '';
          loadTemplates();
        }
      } catch (err) {
        status.className = 'status-msg status-error show';
        status.innerText = `❌ Error: ${err.message}`;
      }
    }

    // Delete Template
    async function deleteTemplate(name) {
      if (!confirm(`Delete template "${name}"?`)) return;
      try {
        await fetch(`/api/templates/${encodeURIComponent(name)}`, { method: 'DELETE' });
        loadTemplates();
      } catch (err) {
        alert(err.message);
      }
    }

    // Load System Status
    async function loadStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        document.getElementById('hdrProvider').innerText = (data.provider || 'twilio').toUpperCase();
        const badge = document.getElementById('hdrStatusBadge');
        if (data.configured) {
          badge.className = 'badge badge-green';
          badge.innerText = 'Connected';
          document.getElementById('configBanner').style.display = 'none';
        } else {
          badge.className = 'badge badge-yellow';
          badge.innerText = 'Needs Keys';
          document.getElementById('configBanner').style.display = 'flex';
          document.getElementById('bannerDetails').innerText = data.warnings ? ` (${data.warnings.join(', ')})` : '';
        }
      } catch (e) {
        console.error(e);
      }
    }

    // Load Stats
    async function loadStats() {
      try {
        const res = await fetch('/api/stats');
        const d = await res.json();
        document.getElementById('statContacts').innerText = d.contacts || 0;
        document.getElementById('statCampaigns').innerText = d.campaigns || 0;
        document.getElementById('statSent').innerText = d.sent || 0;
        document.getElementById('statRate').innerText = `${d.delivery_rate || 0}%`;
      } catch (e) {
        console.error(e);
      }
    }

    // Load Campaigns
    async function loadCampaigns() {
      try {
        const res = await fetch('/api/campaigns');
        campaignsData = await res.json();
        const sel = document.getElementById('sendCampaignSelect');
        sel.innerHTML = '<option value="">-- Choose a Campaign --</option>';
        campaignsData.forEach(c => {
          sel.innerHTML += `<option value="${c.id}">${c.name} (${c.contact_count || 0} contacts) [${c.status}]</option>`;
        });
      } catch (e) {
        console.error(e);
      }
    }

    // Load Contacts
    async function loadContacts() {
      try {
        const res = await fetch('/api/contacts');
        contactsData = await res.json();
        document.getElementById('contactsCountBadge').innerText = contactsData.length;
        const tbody = document.getElementById('contactsTableBody');
        if (contactsData.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No contacts imported yet.</td></tr>';
          return;
        }
        tbody.innerHTML = contactsData.map(c => `
          <tr>
            <td style="font-weight: 600; color: #fff;">${c.phone}</td>
            <td>${c.first_name || '-'}</td>
            <td>${c.last_name || '-'}</td>
            <td style="color: var(--text-muted);">${c.email || '-'}</td>
            <td style="font-size: 0.8rem; color: var(--text-muted);">${(c.created_at || '').substring(0, 16)}</td>
          </tr>
        `).join('');
      } catch (e) {
        console.error(e);
      }
    }

    // Load Message Logs
    async function loadMessages() {
      try {
        const res = await fetch('/api/messages');
        const msgs = await res.json();
        const tbody = document.getElementById('messagesTableBody');
        if (msgs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">No messages logged yet.</td></tr>';
          return;
        }
        tbody.innerHTML = msgs.map(m => {
          const badgeClass = m.status === 'delivered' || m.status === 'read' ? 'badge-green' : m.status === 'sent' ? 'badge-blue' : m.status === 'failed' ? 'badge-red' : 'badge-yellow';
          return `
            <tr>
              <td style="font-weight: 600;">${m.phone}</td>
              <td><span class="badge ${badgeClass}">${m.status}</span></td>
              <td style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${m.content || ''}</td>
              <td style="font-size: 0.8rem; color: var(--text-muted);">${(m.sent_at || '').substring(0, 16)}</td>
              <td style="color: #f87171; font-size: 0.8rem;">${m.error_message || '-'}</td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        console.error(e);
      }
    }

    // Load Templates
    async function loadTemplates() {
      try {
        const res = await fetch('/api/templates');
        templatesData = await res.json();
        
        // Update send template select
        const sel = document.getElementById('sendTemplateSelect');
        sel.innerHTML = '<option value="">-- Write Custom Message --</option>';
        templatesData.forEach(t => {
          sel.innerHTML += `<option value="${t.name}">${t.name} [${t.category}]</option>`;
        });

        // Update templates list
        const list = document.getElementById('templatesList');
        if (templatesData.length === 0) {
          list.innerHTML = '<p class="notice">No templates created yet.</p>';
          return;
        }
        list.innerHTML = templatesData.map(t => `
          <div style="background: var(--input-bg); border: 1px solid var(--card-border); border-radius: 8px; padding: 14px; margin-bottom: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
              <strong>📄 ${t.name} <span class="badge badge-blue">${t.category}</span></strong>
              <button class="btn btn-danger btn-sm" onclick="deleteTemplate('${t.name}')">Delete</button>
            </div>
            <div style="color: var(--text-muted); font-size: 0.88rem; white-space: pre-wrap;">${t.body}</div>
          </div>
        `).join('');
      } catch (e) {
        console.error(e);
      }
    }

    // Update Webhook URL display with current domain
    function updateWebhookUrls() {
      const origin = window.location.origin;
      document.getElementById('codeMetaUrl').innerText = `${origin}/webhook`;
      document.getElementById('codeTwilioUrl').innerText = `${origin}/twilio-status`;
    }

    // Init on page load
    window.addEventListener('DOMContentLoaded', () => {
      updateWebhookUrls();
      loadStatus();
      loadStats();
      loadCampaigns();
      loadContacts();
      loadMessages();
      loadTemplates();
    });
  </script>
</body>
</html>
"""


# -- Root & Web Dashboard Routes -------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, format: str = ""):
    """Serve the Web Dashboard or API root discovery depending on client."""
    accept = request.headers.get("accept", "")
    if format == "json" or ("application/json" in accept and "text/html" not in accept):
        return JSONResponse({
            "name": config.BOT_NAME,
            "version": config.BOT_VERSION,
            "endpoints": {
                "dashboard": "/",
                "health": "/health",
                "webhook_meta": "/webhook",
                "twilio_status": "/twilio-status",
                "docs": "/docs",
                "api_stats": "/api/stats",
                "api_status": "/api/status",
                "api_contacts": "/api/contacts",
                "api_campaigns": "/api/campaigns",
                "api_templates": "/api/templates",
            },
        })
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_view():
    """Explicit /dashboard endpoint serving the Web UI."""
    return HTMLResponse(content=DASHBOARD_HTML)
