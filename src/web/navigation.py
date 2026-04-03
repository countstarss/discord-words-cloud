from __future__ import annotations

APP_NAME = "Rubii Words Cloud"

PRIMARY_NAV_ITEMS = [
    {
        "key": "dashboard",
        "endpoint": "dashboard.index",
        "label": "Dashboard",
        "description": "Live collection and report overview",
        "short": "DB",
    },
    {
        "key": "reports",
        "endpoint": "reports.index",
        "label": "Reports",
        "description": "Browse generated daily reports",
        "short": "RP",
    },
    {
        "key": "messages",
        "endpoint": "messages.index",
        "label": "Messages",
        "description": "Browse collected messages by channel and date",
        "short": "MS",
    },
]
