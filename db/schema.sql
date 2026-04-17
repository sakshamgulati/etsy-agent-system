-- Etsy listings snapshot
CREATE TABLE IF NOT EXISTS listing_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    title TEXT,
    price REAL,
    views INTEGER DEFAULT 0,
    favorites INTEGER DEFAULT 0,
    quantity INTEGER DEFAULT 0,
    state TEXT,
    snapshot_at TEXT NOT NULL  -- ISO 8601 timestamp
);

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE NOT NULL,
    listing_id TEXT,
    amount_paid REAL,
    currency TEXT,
    status TEXT,
    created_at TEXT,
    recorded_at TEXT NOT NULL
);

-- Shop-level stats
CREATE TABLE IF NOT EXISTS shop_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_date TEXT NOT NULL,
    visits INTEGER DEFAULT 0,
    views INTEGER DEFAULT 0,
    orders INTEGER DEFAULT 0,
    revenue REAL DEFAULT 0.0,
    snapshot_at TEXT NOT NULL,
    UNIQUE(stat_date)
);

-- Agent run audit log
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,  -- 'success', 'error', 'running'
    summary TEXT,
    error_detail TEXT
);

-- SEO changes history
CREATE TABLE IF NOT EXISTS seo_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    field TEXT NOT NULL,  -- 'title', 'tags', 'description'
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT NOT NULL,
    approved INTEGER DEFAULT 1  -- auto-applied = 1
);

-- Pricing recommendations
CREATE TABLE IF NOT EXISTS pricing_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    current_price REAL,
    recommended_price REAL,
    rationale TEXT,
    status TEXT DEFAULT 'pending',  -- 'pending', 'approved', 'rejected', 'applied'
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- Ad spend tracking
CREATE TABLE IF NOT EXISTS ad_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    date TEXT NOT NULL,
    spend REAL DEFAULT 0.0,
    clicks INTEGER DEFAULT 0,
    impressions INTEGER DEFAULT 0,
    sales INTEGER DEFAULT 0,
    roas REAL DEFAULT 0.0,
    recorded_at TEXT NOT NULL
);

-- Social media posts
CREATE TABLE IF NOT EXISTS social_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    platform TEXT NOT NULL,  -- 'pinterest', 'instagram'
    post_id TEXT,
    caption TEXT,
    utm_params TEXT,
    posted_at TEXT NOT NULL,
    status TEXT DEFAULT 'published'
);

-- Agent messages / pending approvals (for CEO -> Saksham workflow)
CREATE TABLE IF NOT EXISTS pending_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    action_type TEXT NOT NULL,  -- 'price_change', 'code_patch', 'disable_agent', 'budget_increase'
    payload TEXT NOT NULL,  -- JSON blob with action details
    status TEXT DEFAULT 'pending',  -- 'pending', 'approved', 'rejected'
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
