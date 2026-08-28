-- Hub message id for single-message Telegram menu UX (edit-in-place).
ALTER TABLE telegram_transport_state
  ADD COLUMN IF NOT EXISTS hub_message_id BIGINT;
