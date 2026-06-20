"""
Personality Forge — admin API routes.

Trait CRUD (same interface as base personality skill) plus interview
session management. The interview chat messages flow through gatekeeper's
message queue to the running bot — this API just stores conversation
history and parses extracted traits from bot responses.

Handler signature: async handler(pool, body=None, **regex_groups)
Returns: dict (JSON response). Use __status key for non-200 status codes.
"""

import hashlib
import json
import re
import os

import structlog

log = structlog.get_logger()

VALID_CATEGORIES = {
    'identity', 'tone', 'stance', 'boundary', 'phrase', 'situational',
    'lexicon', 'rhetoric', 'worldview',
}

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))


# =============================================================================
# TRAIT EXTRACTION FROM BOT RESPONSES
# =============================================================================

_TRAIT_BLOCK_RE = re.compile(
    r'```trait\s*\n(.*?)\n```',
    re.DOTALL,
)


def _parse_trait_blocks(text: str) -> list[dict]:
    """Extract structured trait blocks from bot response text."""
    traits = []
    for match in _TRAIT_BLOCK_RE.finditer(text):
        block = match.group(1)
        trait = {}
        for line in block.strip().split('\n'):
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip().lower()
            value = value.strip()
            if key == 'tags':
                trait[key] = [t.strip() for t in value.split(',') if t.strip()]
            elif key == 'weight':
                try:
                    trait[key] = float(value)
                except ValueError:
                    trait[key] = 1.0
            elif key == 'stable':
                trait[key] = value.lower() in ('true', 'yes', '1')
            else:
                trait[key] = value
        if trait.get('content') and trait.get('category'):
            if trait['category'] not in VALID_CATEGORIES:
                trait['category'] = 'tone'  # safe fallback
            traits.append(trait)
    return traits


def _strip_trait_blocks(text: str) -> str:
    """Remove trait code blocks from text, leaving the conversational parts."""
    return _TRAIT_BLOCK_RE.sub('', text).strip()


# =============================================================================
# TRAIT CRUD
# =============================================================================

async def list_traits(pool, body=None, slug=None, **kw):
    """List all personality traits for a profile."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, category, subcategory, content, tags, weight, stable, source,
                      created_at, updated_at
               FROM personality_forge_traits
               WHERE profile_slug = $1
               ORDER BY stable DESC, category, weight DESC""",
            slug,
        )
    traits = [
        {
            "id": str(row['id']),
            "category": row['category'],
            "subcategory": row['subcategory'],
            "content": row['content'],
            "tags": row['tags'] or [],
            "weight": row['weight'],
            "stable": row['stable'],
            "source": row['source'],
            "created_at": row['created_at'].isoformat(),
            "updated_at": row['updated_at'].isoformat(),
        }
        for row in rows
    ]
    return {"profile_slug": slug, "count": len(traits), "traits": traits}


async def create_trait(pool, body=None, slug=None, **kw):
    """Create a new personality trait."""
    if not body:
        return {"__status": 400, "detail": "Request body required"}

    content = (body.get('content') or '').strip()
    if not content:
        return {"__status": 400, "detail": "content is required"}

    category = body.get('category', 'tone')
    if category not in VALID_CATEGORIES:
        return {"__status": 400, "detail": f"category must be one of: {VALID_CATEGORIES}"}

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    tags = body.get('tags', [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]

    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO personality_forge_traits
                    (profile_slug, category, subcategory, content, content_hash, tags, weight, stable, source)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   RETURNING id""",
                slug,
                category,
                body.get('subcategory'),
                content,
                content_hash,
                tags,
                float(body.get('weight', 1.0)),
                bool(body.get('stable', False)),
                body.get('source', 'interview'),
            )
            return {"id": str(row['id']), "status": "created"}
        except Exception as e:
            if "unique" in str(e).lower():
                return {"__status": 409, "detail": "Duplicate trait content"}
            raise


async def delete_trait(pool, body=None, slug=None, trait_id=None, **kw):
    """Delete a personality trait."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM personality_forge_traits WHERE id = $1::uuid AND profile_slug = $2",
            trait_id, slug,
        )
    if result == "DELETE 0":
        return {"__status": 404, "detail": "Trait not found"}
    return {"status": "deleted"}


async def update_trait(pool, body=None, slug=None, trait_id=None, **kw):
    """Update an existing trait."""
    if not body:
        return {"__status": 400, "detail": "Request body required"}

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM personality_forge_traits WHERE id = $1::uuid AND profile_slug = $2",
            trait_id, slug,
        )
        if not existing:
            return {"__status": 404, "detail": "Trait not found"}

        updates = []
        params = [trait_id, slug]
        idx = 3

        for field in ('content', 'category', 'subcategory'):
            if field in body:
                val = body[field].strip() if isinstance(body[field], str) else body[field]
                updates.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1
                if field == 'content':
                    updates.append(f"content_hash = ${idx}")
                    params.append(hashlib.sha256(val.encode()).hexdigest())
                    idx += 1

        if 'tags' in body:
            tags = body['tags']
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(',') if t.strip()]
            updates.append(f"tags = ${idx}")
            params.append(tags)
            idx += 1

        if 'weight' in body:
            updates.append(f"weight = ${idx}")
            params.append(float(body['weight']))
            idx += 1

        if 'stable' in body:
            updates.append(f"stable = ${idx}")
            params.append(bool(body['stable']))
            idx += 1

        if not updates:
            return {"status": "no changes"}

        updates.append("updated_at = NOW()")
        sql = f"UPDATE personality_forge_traits SET {', '.join(updates)} WHERE id = $1::uuid AND profile_slug = $2"
        await conn.execute(sql, *params)

    return {"status": "updated"}


async def import_traits(pool, body=None, slug=None, **kw):
    """Import traits from YAML data."""
    if not body:
        return {"__status": 400, "detail": "Request body required"}

    import yaml
    yaml_content = body.get('yaml', '')
    if not yaml_content:
        return {"__status": 400, "detail": "yaml field is required"}

    try:
        parsed = yaml.safe_load(yaml_content)
    except Exception as e:
        return {"__status": 400, "detail": f"Invalid YAML: {e}"}

    traits_data = parsed.get('traits', [])
    if not traits_data:
        return {"__status": 400, "detail": "No traits found in YAML"}

    created = 0
    skipped = 0
    async with pool.acquire() as conn:
        for t in traits_data:
            content = (t.get('content') or '').strip()
            if not content:
                continue
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            tags = t.get('tags', [])
            try:
                await conn.execute(
                    """INSERT INTO personality_forge_traits
                        (profile_slug, category, subcategory, content, content_hash, tags, weight, stable, source)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'import')
                       ON CONFLICT (profile_slug, content_hash) DO NOTHING""",
                    slug,
                    t.get('category', 'tone'),
                    t.get('subcategory'),
                    content,
                    content_hash,
                    tags,
                    float(t.get('weight', 1.0)),
                    bool(t.get('stable', False)),
                )
                created += 1
            except Exception:
                skipped += 1

    return {"created": created, "skipped": skipped}


# =============================================================================
# INTERVIEW SESSIONS
# =============================================================================

async def get_session(pool, body=None, slug=None, **kw):
    """Get or create the active interview session for a profile."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, messages, categories_covered, trait_count, status, created_at, updated_at
               FROM personality_forge_sessions
               WHERE profile_slug = $1 AND status = 'active'
               ORDER BY updated_at DESC LIMIT 1""",
            slug,
        )

    if row:
        return {
            "id": str(row['id']),
            "messages": json.loads(row['messages']) if isinstance(row['messages'], str) else row['messages'],
            "categories_covered": row['categories_covered'] or [],
            "trait_count": row['trait_count'],
            "status": row['status'],
            "created_at": row['created_at'].isoformat(),
            "updated_at": row['updated_at'].isoformat(),
        }

    # No active session — create one
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO personality_forge_sessions (profile_slug, messages)
               VALUES ($1, '[]'::jsonb)
               RETURNING id, created_at""",
            slug,
        )

    return {
        "id": str(row['id']),
        "messages": [],
        "categories_covered": [],
        "trait_count": 0,
        "status": "active",
        "created_at": row['created_at'].isoformat(),
        "updated_at": row['created_at'].isoformat(),
    }


async def save_message(pool, body=None, slug=None, **kw):
    """
    Save a message exchange to the interview session.

    Called by gatekeeper after the bot responds. Includes both the user
    message and the bot response. Parses trait blocks from the bot response
    and saves them automatically.
    """
    if not body:
        return {"__status": 400, "detail": "Request body required"}

    session_id = body.get('session_id')
    user_message = body.get('user_message', '')
    bot_response = body.get('bot_response', '')

    if not session_id:
        return {"__status": 400, "detail": "session_id required"}

    # Parse traits from bot response
    extracted_traits = _parse_trait_blocks(bot_response)
    clean_response = _strip_trait_blocks(bot_response)

    # Save extracted traits
    saved_count = 0
    for trait in extracted_traits:
        content = trait.get('content', '').strip()
        if not content:
            continue
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """INSERT INTO personality_forge_traits
                        (profile_slug, category, subcategory, content, content_hash, tags, weight, stable, source)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'interview')
                       ON CONFLICT (profile_slug, content_hash) DO NOTHING""",
                    slug,
                    trait.get('category', 'tone'),
                    trait.get('subcategory'),
                    content,
                    content_hash,
                    trait.get('tags', []),
                    float(trait.get('weight', 1.0)),
                    bool(trait.get('stable', False)),
                )
                saved_count += 1
            except Exception as e:
                log.warning("forge_trait_save_failed", error=str(e))

    # Update session with new messages
    new_messages = []
    if user_message:
        new_messages.append({"role": "user", "content": user_message})
    if clean_response:
        new_messages.append({"role": "assistant", "content": clean_response})

    # Track which categories have been covered
    new_categories = list({t['category'] for t in extracted_traits})

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE personality_forge_sessions
               SET messages = messages || $2::jsonb,
                   categories_covered = (
                       SELECT array_agg(DISTINCT c)
                       FROM unnest(categories_covered || $3::text[]) AS c
                   ),
                   trait_count = trait_count + $4,
                   updated_at = NOW()
               WHERE id = $1::uuid""",
            session_id,
            json.dumps(new_messages),
            new_categories,
            saved_count,
        )

    log.info("forge_message_saved", slug=slug, traits_extracted=saved_count,
             categories=new_categories)

    return {
        "status": "saved",
        "traits_extracted": saved_count,
        "traits": extracted_traits,
        "clean_response": clean_response,
    }


async def reset_session(pool, body=None, slug=None, **kw):
    """Archive the current session and start fresh."""
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE personality_forge_sessions
               SET status = 'archived'
               WHERE profile_slug = $1 AND status = 'active'""",
            slug,
        )
    return {"status": "reset"}


async def get_interview_prompt(pool, body=None, slug=None, **kw):
    """
    Return the interview system prompt with current trait context.

    Called by gatekeeper to build the message sent to the bot.
    Includes existing traits so the bot knows what's already been covered.
    """
    # Load the interview prompt template
    prompt_path = os.path.join(_SKILL_DIR, 'interview_prompt.md')
    with open(prompt_path) as f:
        base_prompt = f.read()

    # Load existing traits for context
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT category, content, tags, weight, stable
               FROM personality_forge_traits
               WHERE profile_slug = $1
               ORDER BY category, stable DESC, weight DESC""",
            slug,
        )

    if rows:
        traits_context = "\n## Existing Traits (already discovered)\n\n"
        by_cat = {}
        for r in rows:
            cat = r['category']
            if cat not in by_cat:
                by_cat[cat] = []
            by_cat[cat].append(r['content'])
        for cat, contents in by_cat.items():
            traits_context += f"**{cat}** ({len(contents)} traits):\n"
            for c in contents:
                traits_context += f"- {c}\n"
            traits_context += "\n"
        traits_context += "Do NOT re-discover these. Focus on gaps and deeper detail.\n"
    else:
        traits_context = "\n## Existing Traits\n\nNone yet — this is a fresh start.\n"

    return {
        "prompt": base_prompt + traits_context,
        "trait_count": len(rows),
        "categories_covered": list({r['category'] for r in rows}),
    }


# =============================================================================
# ROUTE TABLE
# =============================================================================

routes = [
    # Trait CRUD
    ("GET",    r"/(?P<slug>[\w-]+)/traits$",                        list_traits),
    ("POST",   r"/(?P<slug>[\w-]+)/traits$",                        create_trait),
    ("POST",   r"/(?P<slug>[\w-]+)/traits/(?P<trait_id>[\w-]+)$",   update_trait),
    ("DELETE", r"/(?P<slug>[\w-]+)/traits/(?P<trait_id>[\w-]+)$",   delete_trait),
    ("POST",   r"/(?P<slug>[\w-]+)/import$",                        import_traits),
    # Interview session
    ("GET",    r"/(?P<slug>[\w-]+)/session$",                        get_session),
    ("POST",   r"/(?P<slug>[\w-]+)/session/message$",               save_message),
    ("POST",   r"/(?P<slug>[\w-]+)/session/reset$",                 reset_session),
    ("GET",    r"/(?P<slug>[\w-]+)/interview-prompt$",              get_interview_prompt),
]
