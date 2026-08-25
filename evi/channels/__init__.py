"""Chat channels — reach eVi from somewhere other than its own UI.

A channel is an inbound message source (today: Telegram) that hands text to the
agent and sends the reply back. Channels are OFF unless configured, and every
one of them is expected to gate unknown senders before any agent turn runs — an
inbound channel drives a tool-capable assistant, so an open one is remote code
execution on the user's machine.
"""
