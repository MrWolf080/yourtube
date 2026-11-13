CREATE TABLE IF NOT EXISTS accounts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    handle text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'user' CHECK (role IN ('user','moderator','admin'))
);
CREATE TABLE IF NOT EXISTS sessions (
    digest text PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    expires timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
    id uuid PRIMARY KEY,
    owner_id bigint NOT NULL REFERENCES accounts,
    title text NOT NULL,
    description text NOT NULL,
    category text NOT NULL,
    private boolean NOT NULL DEFAULT false,
    hidden boolean NOT NULL DEFAULT false,
    duration integer NOT NULL CHECK (duration >= 0),
    views bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS videos_feed ON videos (created_at DESC) WHERE NOT private AND NOT hidden;
CREATE INDEX IF NOT EXISTS videos_owner ON videos (owner_id, created_at DESC);
CREATE TABLE IF NOT EXISTS grants (
    video_id uuid NOT NULL REFERENCES videos ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    PRIMARY KEY (video_id, user_id)
);
CREATE TABLE IF NOT EXISTS likes (
    video_id uuid NOT NULL REFERENCES videos ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    PRIMARY KEY (video_id, user_id)
);
CREATE TABLE IF NOT EXISTS comments (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES accounts,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS comments_video ON comments (video_id, id DESC);
CREATE TABLE IF NOT EXISTS subscriptions (
    channel_id bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    PRIMARY KEY (channel_id, user_id),
    CHECK (channel_id <> user_id)
);
CREATE TABLE IF NOT EXISTS playlists (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id bigint NOT NULL REFERENCES accounts ON DELETE CASCADE,
    title text NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id bigint NOT NULL REFERENCES playlists ON DELETE CASCADE,
    video_id uuid NOT NULL REFERENCES videos ON DELETE CASCADE,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (playlist_id, video_id)
);
CREATE TABLE IF NOT EXISTS reports (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES videos ON DELETE CASCADE,
    reporter_id bigint NOT NULL REFERENCES accounts,
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','dismissed','hidden')),
    reviewed_by bigint REFERENCES accounts,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_report ON reports (video_id, reporter_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS video_view_receipts (
    video_id uuid NOT NULL REFERENCES videos ON DELETE CASCADE,
    viewer text NOT NULL,
    viewed_at timestamptz NOT NULL,
    PRIMARY KEY (video_id, viewer)
);

ALTER TABLE likes ADD COLUMN IF NOT EXISTS liked_at timestamptz;
ALTER TABLE likes ALTER COLUMN liked_at SET DEFAULT now();
CREATE INDEX IF NOT EXISTS likes_by_user ON likes (user_id, liked_at DESC NULLS LAST);

ALTER TABLE video_view_receipts ADD COLUMN IF NOT EXISTS last_watched_at timestamptz;
CREATE INDEX IF NOT EXISTS video_view_receipts_by_viewer
    ON video_view_receipts (viewer, (coalesce(last_watched_at, viewed_at)) DESC);

CREATE TABLE IF NOT EXISTS upload_attempts (
    user_id bigint PRIMARY KEY REFERENCES accounts ON DELETE CASCADE,
    started_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    scope text NOT NULL,
    subject text NOT NULL,
    attempts integer NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (scope, subject)
);
CREATE INDEX IF NOT EXISTS auth_rate_limits_expiry ON auth_rate_limits (expires_at);

CREATE TABLE IF NOT EXISTS pending_video_deletions (
    video_id uuid PRIMARY KEY,
    last_attempt_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'ready'
    CHECK (status IN ('queued','processing','ready','failed'));
ALTER TABLE videos ADD COLUMN IF NOT EXISTS progress integer NOT NULL DEFAULT 100
    CHECK (progress BETWEEN 0 AND 100);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS lease_token uuid;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS reserved_bytes bigint;
ALTER TABLE pending_video_deletions ADD COLUMN IF NOT EXISTS reserved_bytes bigint;
CREATE INDEX IF NOT EXISTS videos_queue ON videos (created_at,id)
    WHERE status IN ('queued','processing');
ALTER TABLE videos ADD COLUMN IF NOT EXISTS cleanup_pending boolean NOT NULL DEFAULT false;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS retry_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS guest_view_receipts_expiry ON video_view_receipts (viewed_at) WHERE viewer LIKE 'guest:%' OR viewer LIKE 'browser:%';
