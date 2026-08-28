"""Output formatting — the only layer allowed to produce Telegram-facing
text. Graph nodes, tools, and repositories return data; presenters.py turns
data into a UserFacingResponse, and chunking.py turns that into concrete
TelegramOutboundMessage rows. Nothing upstream of this package should format
Telegram prose by hand.
"""
