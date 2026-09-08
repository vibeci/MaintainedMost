-- Update notification list. A row exists from the moment someone asks, but
-- only a confirmed row is ever mailed.
CREATE TABLE IF NOT EXISTS subscribers (
  email        TEXT PRIMARY KEY,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | unsubscribed
  token        TEXT NOT NULL,                    -- confirm and unsubscribe both use it
  source       TEXT,                             -- which part of the site they came from
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  confirmed_at TEXT,
  ip_hash      TEXT                              -- for rate limiting, not identification
);

CREATE INDEX IF NOT EXISTS subscribers_status ON subscribers (status);
CREATE INDEX IF NOT EXISTS subscribers_token ON subscribers (token);
