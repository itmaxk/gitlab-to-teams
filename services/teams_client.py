import json

import httpx


def _build_adaptive_card(
    mr_title: str,
    mr_url: str,
    file_path: str,
    file_content: str,
    rule_name: str,
) -> dict:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"🔔 {rule_name}",
                            "weight": "Bolder",
                            "size": "Large",
                            "color": "Attention",
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "MR:", "value": mr_title},
                                {"title": "Правило:", "value": rule_name},
                                {"title": "Файл:", "value": file_path},
                            ],
                        },
                        {
                            "type": "TextBlock",
                            "text": file_content,
                            "wrap": True,
                            "fontType": "Monospace",
                            "separator": True,
                        },
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Открыть MR в GitLab",
                            "url": mr_url,
                        }
                    ],
                },
            }
        ],
    }


async def send_teams_notification(
    webhook_url: str,
    mr_title: str,
    mr_url: str,
    file_path: str,
    file_content: str,
    rule_name: str,
) -> None:
    card = _build_adaptive_card(mr_title, mr_url, file_path, file_content, rule_name)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            webhook_url,
            content=json.dumps(card),
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
