"""Dashboard HTML served as a Python string constant."""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Conversation Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f0f2f5; color: #1a1a1a; padding: 16px; }
        .header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
        .header h1 { font-size: 1.4rem; }
        .stat { background: #fff; padding: 12px 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); font-size: 0.95rem; }
        .stat strong { font-size: 1.3rem; }
        #conversations { display: flex; flex-direction: column; gap: 16px; }
        .conversation { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; }
        .conv-header { background: #075e54; color: #fff; padding: 10px 16px; font-size: 0.95rem; font-weight: 600; }
        .conv-messages { padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; }
        .msg { padding: 8px 12px; border-radius: 8px; max-width: 85%; font-size: 0.9rem; line-height: 1.4; word-wrap: break-word; }
        .msg.incoming { background: #e8e8e8; align-self: flex-start; }
        .msg.outgoing { background: #dcf8c6; align-self: flex-end; }
        .msg .meta { font-size: 0.75rem; color: #777; margin-top: 4px; }
        .empty { text-align: center; color: #888; padding: 40px; font-size: 1.1rem; }
        .refresh-note { font-size: 0.8rem; color: #999; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Conversations &mdash; last 6 hours</h1>
        <div style="display:flex;align-items:center;gap:16px;">
            <span class="refresh-note">Auto-refreshes every 5s</span>
            <div class="stat">Contacts: <strong id="contact-count">0</strong></div>
        </div>
    </div>
    <div id="conversations"><div class="empty">Loading&hellip;</div></div>

    <script>
        const token = new URLSearchParams(window.location.search).get("token");

        function escapeHtml(str) {
            const div = document.createElement("div");
            div.textContent = str;
            return div.innerHTML;
        }

        async function refresh() {
            try {
                const res = await fetch("/dashboard/api/messages?token=" + encodeURIComponent(token));
                if (!res.ok) return;
                const data = await res.json();

                document.getElementById("contact-count").textContent = data.contacts;

                const grouped = {};
                data.messages.forEach(m => {
                    if (!grouped[m.phone]) grouped[m.phone] = [];
                    grouped[m.phone].push(m);
                });

                const container = document.getElementById("conversations");
                const phones = Object.keys(grouped);

                if (phones.length === 0) {
                    container.innerHTML = '<div class="empty">No conversations in the last 6 hours.</div>';
                    return;
                }

                container.innerHTML = "";
                phones.forEach(phone => {
                    const conv = document.createElement("div");
                    conv.className = "conversation";

                    const header = document.createElement("div");
                    header.className = "conv-header";
                    header.textContent = phone.replace("whatsapp:", "");
                    conv.appendChild(header);

                    const msgs = document.createElement("div");
                    msgs.className = "conv-messages";
                    grouped[phone].forEach(m => {
                        const div = document.createElement("div");
                        div.className = "msg " + m.direction;
                        const time = new Date(m.created_at).toLocaleTimeString();
                        div.innerHTML = escapeHtml(m.body) + '<div class="meta">' + m.direction + " &middot; " + time + "</div>";
                        msgs.appendChild(div);
                    });
                    conv.appendChild(msgs);
                    container.appendChild(conv);
                });
            } catch (e) {
                console.error("Dashboard refresh failed:", e);
            }
        }

        refresh();
        setInterval(refresh, 5000);
    </script>
</body>
</html>"""
