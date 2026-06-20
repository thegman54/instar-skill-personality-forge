-- Personality Forge schema
-- Traits table (same structure as base personality skill for compatibility)
-- Plus interview sessions for persistent multi-turn conversations

CREATE TABLE IF NOT EXISTS personality_forge_traits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_slug VARCHAR(255) NOT NULL,
    category VARCHAR(50) NOT NULL,
    subcategory VARCHAR(100),
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    tags TEXT[] DEFAULT '{}',
    weight FLOAT DEFAULT 1.0,
    stable BOOLEAN DEFAULT FALSE,
    source VARCHAR(255) DEFAULT 'manual',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(profile_slug, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_forge_traits_profile_cat
    ON personality_forge_traits(profile_slug, category);
CREATE INDEX IF NOT EXISTS idx_forge_traits_profile_weight
    ON personality_forge_traits(profile_slug, weight DESC);
CREATE INDEX IF NOT EXISTS idx_forge_traits_tags
    ON personality_forge_traits USING GIN(tags);

-- Interview sessions — persistent conversation history for the forge chat
CREATE TABLE IF NOT EXISTS personality_forge_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_slug VARCHAR(255) NOT NULL,
    messages JSONB NOT NULL DEFAULT '[]',
    categories_covered TEXT[] DEFAULT '{}',
    trait_count INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_forge_sessions_slug
    ON personality_forge_sessions(profile_slug, status);
