"""Migration 0002 — pg_trgm extension + the maintained search-index triggers.

THE PROMPT-14 PITFALL, guarded here: the SearchDocument tsvector is built by
DATABASE TRIGGERS created in THIS one migration — never inline per query,
and never via save() hooks inside apps.guides/posts/qa/profiles because the
one-agent-per-file isolation rule forbids touching their models. SQL
triggers touch only their tables, which the contract explicitly allows.

Trigger coverage (each table gets BEFORE-safe AFTER handlers below):
    guides_guide            insert / update of status / delete
    guides_guiderevision    any write → refresh the owning guide
    guides_guidestep        any write → refresh the owning guide
    posts_post              insert / update / delete
    qa_question             insert / update / delete
    qa_answer               any write → refresh the parent question
    accounts_user           insert / update (suspension, restore, username) / delete
    profiles_profile        insert / update / delete

Status and privacy rules baked into the functions:
    - REMOVED content, guides without a published revision, non-PUBLISHED
      posts/questions, and content of suspended (is_active=False) authors is
      never indexed (search only shows real, visible rows). Reactivating an
      account re-indexes that member's content, so the index honestly
      regains what moderation temporarily hid.
    - People documents are built ONLY from profiles' public allow-list
      fields: display_name / username, public bio / skills, and the ward
      label when the bio/location groups are public AND
      LocationVisibility == WARD (ward is a coarse label, never a
      coordinate). Phone and e-mail columns are never even read here.
"""
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations

_TABLE = "search_searchdocument"

TRIGGER_SQL = f"""
-- Two GIN indexes for the whole feature: one over the MAINTAINED tsvector
-- column (it carries 'english'-stemmed AND 'simple' lexemes, so en + sw
-- queries share one index), one trigram over the plain-text body column.
CREATE INDEX IF NOT EXISTS search_document_vector_gin
    ON {_TABLE} USING gin (vector);
CREATE INDEX IF NOT EXISTS search_document_body_trgm
    ON {_TABLE} USING gin (body gin_trgm_ops);
-- Titles are trigram-searchable too (% operator in the query predicate);
-- without these index branches a typo'd title falls back to body-only.
CREATE INDEX IF NOT EXISTS search_document_title_en_trgm
    ON {_TABLE} USING gin (title_en gin_trgm_ops);
CREATE INDEX IF NOT EXISTS search_document_title_sw_trgm
    ON {_TABLE} USING gin (title_sw gin_trgm_ops);

-- ----------------------------------------------------------------- helpers --

-- WHY a helper: every upsert branch needs the same ON CONFLICT write; one
-- function means the maintenance contract is typed ONCE.
CREATE OR REPLACE FUNCTION search_set_doc(
    p_kind text, p_app text, p_label text, p_pk bigint,
    p_title_en text, p_title_sw text, p_body text, p_vector tsvector,
    p_author bigint, p_ward text, p_url text, p_created timestamptz
) RETURNS void LANGUAGE plpgsql AS $search_set_doc$
BEGIN
    INSERT INTO {_TABLE}
        (kind, app_label, label, object_pk, title_en, title_sw, body,
         vector, author_id, ward, url, created_at, updated_at)
    VALUES
        (p_kind, p_app, p_label, p_pk::text, COALESCE(p_title_en, ''),
         COALESCE(p_title_sw, ''), COALESCE(p_body, ''), p_vector,
         p_author, COALESCE(p_ward, ''), p_url, p_created,
         statement_timestamp())
    ON CONFLICT (app_label, object_pk) DO UPDATE SET
        kind = EXCLUDED.kind, label = EXCLUDED.label,
        title_en = EXCLUDED.title_en, title_sw = EXCLUDED.title_sw,
        body = EXCLUDED.body, vector = EXCLUDED.vector,
        author_id = EXCLUDED.author_id, ward = EXCLUDED.ward,
        url = EXCLUDED.url, created_at = EXCLUDED.created_at,
        updated_at = EXCLUDED.updated_at;
END
$search_set_doc$;

-- A "blocked author" site-wide is a suspended account: is_active=False is
-- exactly the state moderation's two-actor Suspension gate leaves behind.
CREATE OR REPLACE FUNCTION search_author_blocked(p_author bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT a.id IS NOT NULL AND a.is_active = false
      FROM accounts_user a WHERE a.id = p_author;
$$;

CREATE OR REPLACE FUNCTION search_delete_doc(p_app text, p_pk bigint)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM {_TABLE}
     WHERE app_label = p_app AND object_pk = p_pk::text;
END
$$;

-- ------------------------------------------------------------------ guides --

-- WHY revision-scoped: displayed guide content lives ONLY on the newest
-- PUBLISHED revision (apps.guides revision safety) + its snapshots' steps,
-- so the document is rebuilt from that revision; a guide with no published
-- revision is not a public page and is deleted from the index entirely.
CREATE OR REPLACE FUNCTION search_upsert_guide(p_guide bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    g_status text; g_author bigint; g_created timestamptz;
    rev RECORD; v_steps text; v_body text; v_vector tsvector;
BEGIN
    SELECT status, created_by_id, created_at
      INTO g_status, g_author, g_created
      FROM guides_guide WHERE id = p_guide;
    IF NOT FOUND OR g_status = 'REMOVED' THEN
        PERFORM search_delete_doc('guides', p_guide);
        RETURN;
    END IF;

    -- "Latest published revision" = the highest version with status PUBLISHED.
    SELECT r.id, r.title_en, r.title_sw, r.safety_warning
      INTO rev
      FROM guides_guiderevision r
     WHERE r.guide_id = p_guide AND r.status = 'PUBLISHED'
     ORDER BY r.version DESC
     LIMIT 1;
    IF NOT FOUND THEN
        PERFORM search_delete_doc('guides', p_guide);
        RETURN;
    END IF;

    SELECT COALESCE(string_agg(s.text, E'\\n' ORDER BY s.position), '')
      INTO v_steps
      FROM guides_guidestep s
     WHERE s.revision_id = rev.id;

    v_body := concat_ws(E'\\n', rev.title_en, rev.safety_warning, v_steps);

    IF search_author_blocked(g_author) THEN
        PERFORM search_delete_doc('guides', p_guide);
        RETURN;
    END IF;

    PERFORM search_set_doc('guide', 'guides', 'Guide', p_guide,
        rev.title_en, rev.title_sw, v_body,
        -- Bilingual vector: 'english' lexemes for en content, 'simple'
        -- lexemes for the Kiswahili title + the same body (the sw title
        -- gets BOTH configs so either query configuration reaches it).
        setweight(to_tsvector('english', rev.title_en), 'A')
        || setweight(to_tsvector('simple',  COALESCE(rev.title_sw, '')), 'A')
        || setweight(to_tsvector('english', COALESCE(v_body, '')), 'B')
        || setweight(to_tsvector('simple',  COALESCE(v_body, '')), 'B'),
        g_author, '', '/guides/' || p_guide::text || '/', g_created);
END
$$;

-- ------------------------------------------------------------------- posts --

-- Posts have no title column (text-only posts) — the title is the first
-- 80 characters snapshot at index time; the full text is the trigram body.
CREATE OR REPLACE FUNCTION search_upsert_post(p_post bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_status text; v_text text; v_author bigint; v_created timestamptz;
    v_title text;
BEGIN
    SELECT status, text, author_id, created_at
      INTO v_status, v_text, v_author, v_created
      FROM posts_post WHERE id = p_post;
    IF NOT FOUND OR v_status <> 'PUBLISHED'
       OR search_author_blocked(v_author) THEN
        PERFORM search_delete_doc('posts', p_post);
        RETURN;
    END IF;

    v_title := COALESCE(left(v_text, 80), '');
    PERFORM search_set_doc('post', 'posts', 'Post', p_post,
        v_title, '', v_text,
        setweight(to_tsvector('simple', v_title), 'A')
        || setweight(to_tsvector('simple',  v_text), 'B')
        || setweight(to_tsvector('english', v_text), 'B'),
        v_author, '', '/posts/' || p_post::text || '/', v_created);
END
$$;

-- ------------------------------------------------------------------ Q&A --

-- One document per question; answer bodies are part of the question's
-- searchable body (the qa list-search contract searches them too), so the
-- qa_answer trigger refreshes the PARENT question's document.
CREATE OR REPLACE FUNCTION search_upsert_question(p_q bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    r RECORD; v_author bigint; v_answers text; v_body text; v_vector tsvector;
    v_en_part text;
BEGIN
    SELECT q.title_en, q.title_sw, q.body, q.status, q.asked_by_id, q.created_at
      INTO r FROM qa_question q WHERE q.id = p_q;
    IF NOT FOUND OR r.status <> 'PUBLISHED'
       OR search_author_blocked(r.asked_by_id) THEN
        PERFORM search_delete_doc('qa', p_q);
        RETURN;
    END IF;

    SELECT COALESCE(string_agg(a.body, E'\\n' ORDER BY a.created_at), '')
      INTO v_answers
      FROM qa_answer a WHERE a.question_id = p_q;

    v_body := concat_ws(E'\\n', r.body, v_answers);
    v_en_part := concat_ws(E'\\n', r.title_en, v_body);

    v_vector :=
        setweight(to_tsvector('english', r.title_en), 'A')
        || setweight(to_tsvector('simple',  COALESCE(r.title_sw, '')), 'A')
        || setweight(to_tsvector('english', COALESCE(v_en_part, '')), 'B')
        || setweight(to_tsvector('simple',  concat_ws(E'\\n',
                COALESCE(r.title_sw, ''), COALESCE(r.title_en, ''),
                COALESCE(v_body, ''))), 'B');

    PERFORM search_set_doc('question', 'qa', 'Question', p_q,
        r.title_en, r.title_sw, concat_ws(E'\\n', r.title_en,
            COALESCE(r.title_sw, ''), v_body),
        v_vector, r.asked_by_id, '',
        '/qa/' || p_q::text || '/', r.created_at);
END
$$;

-- ------------------------------------------------------------------ people --

-- Public allow-list fields ONLY (profiles.services.PUBLIC_KEYS semantics).
-- The field_visibility JSON + LocationVisibility ladder are honoured here
-- exactly as profiles.public_data honours them for the profile PAGE.
CREATE OR REPLACE FUNCTION search_upsert_person(p_user bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_username text; v_active boolean;
    p_display text; p_bio text; p_skills jsonb; p_ward text; p_vis text;
    p_fv jsonb; p_created timestamptz;
    v_name text; v_bio text; v_skills text; v_ward text;
    v_body text;
BEGIN
    SELECT username, is_active
      INTO v_username, v_active
      FROM accounts_user WHERE id = p_user;
    IF NOT FOUND OR NOT v_active THEN
        PERFORM search_delete_doc('profiles', p_user);
        RETURN;
    END IF;

    -- NOTE: FOUND-guard, not a RECORD deref — a member can have a real
    -- account before their profile row exists (profiles are created
    -- lazily); such a member is still findable by username alone.
    SELECT display_name, bio, skills, ward, location_visibility,
           field_visibility, created_at
      INTO p_display, p_bio, p_skills, p_ward, p_vis, p_fv, p_created
      FROM profiles_profile WHERE user_id = p_user;

    IF NOT FOUND THEN
        p_display := ''; p_bio := ''; p_skills := NULL; p_ward := '';
        p_vis := 'WARD'; p_fv := NULL;  -- jsonb NULL ≡ every group 'public'
    END IF;

    v_name := COALESCE(NULLIF(COALESCE(p_display, ''), ''), v_username);
    -- If a profile edit has made a group non-public it must drop out of the
    -- public search index the same moment it drops off the profile page.
    IF COALESCE(p_fv ->> 'bio', 'public') = 'public' THEN
        v_bio := COALESCE(p_bio, '');
    ELSE
        v_bio := '';
    END IF;
    IF COALESCE(p_fv ->> 'skills', 'public') = 'public' THEN
        v_skills := COALESCE(p_skills::text, '');
    ELSE
        v_skills := '';
    END IF;
    -- KDPA: the ward is a coarse LABEL and only public-WARD members expose
    -- it; NEIGHBOURHOOD/CITY visibility degrades to no ward in search.
    IF COALESCE(p_fv ->> 'location', 'public') = 'public'
       AND p_vis = 'WARD'
       AND COALESCE(p_ward, '') <> '' THEN
        v_ward := p_ward;
    ELSE
        v_ward := '';
    END IF;

    v_body := concat_ws(E'\\n', v_username, v_name, v_bio, v_skills, v_ward);

    PERFORM search_set_doc('person', 'profiles', 'Profile', p_user,
        v_name, '', v_body,
        setweight(to_tsvector('simple', v_name), 'A')
        || setweight(to_tsvector('simple', v_body), 'B'),
        p_user, v_ward,
        '/profiles/u/' || v_username || '/',
        COALESCE(p_created, statement_timestamp()));
END
$$;

-- ----------------------------------------------------------------- dispatch --

-- ONE plpgsql dispatcher shared by every trigger, branching on the firing
-- table (TG_TABLE_NAME) and operation (TG_OP). WHY: eight tiny triggers with
-- one body — future maintainers read a single maintenance contract.
CREATE OR REPLACE FUNCTION search_refresh() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_guide bigint;
    v_item bigint;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'guides_guide' THEN
            IF TG_OP = 'DELETE' THEN
                PERFORM search_delete_doc('guides', OLD.id);
            ELSE
                PERFORM search_upsert_guide(NEW.id);
            END IF;

        WHEN 'guides_guiderevision' THEN
            IF TG_OP = 'DELETE' THEN
                -- Newest PUBLISHED revision may be gone now — recompute.
                PERFORM search_upsert_guide(OLD.guide_id);
            ELSE
                PERFORM search_upsert_guide(NEW.guide_id);
            END IF;

        WHEN 'guides_guidestep' THEN
            IF TG_OP = 'DELETE' THEN
                SELECT guide_id INTO v_guide FROM guides_guiderevision
                 WHERE id = OLD.revision_id;
            ELSE
                SELECT guide_id INTO v_guide FROM guides_guiderevision
                 WHERE id = NEW.revision_id;
            END IF;
            -- Missing revision → its guide is being torn down cascade-style;
            -- upsert handles NOT FOUND by deleting nothing (no doc existed).
            IF v_guide IS NOT NULL THEN PERFORM search_upsert_guide(v_guide); END IF;

        WHEN 'posts_post' THEN
            IF TG_OP = 'DELETE' THEN
                PERFORM search_delete_doc('posts', OLD.id);
            ELSE
                PERFORM search_upsert_post(NEW.id);
            END IF;

        WHEN 'qa_question' THEN
            IF TG_OP = 'DELETE' THEN
                PERFORM search_delete_doc('qa', OLD.id);
            ELSE
                PERFORM search_upsert_question(NEW.id);
            END IF;

        WHEN 'qa_answer' THEN
            IF TG_OP = 'DELETE' THEN
                PERFORM search_upsert_question(OLD.question_id);
            ELSE
                PERFORM search_upsert_question(NEW.question_id);
            END IF;

        WHEN 'profiles_profile' THEN
            -- Profile write → rebuild from the user row alone on DELETE
            -- (profile rows may vanish before the user does).
            IF TG_OP = 'DELETE' THEN
                PERFORM search_upsert_person(OLD.user_id);
            ELSE
                PERFORM search_upsert_person(NEW.user_id);
            END IF;

        WHEN 'accounts_user' THEN
            IF TG_OP = 'DELETE' THEN
                PERFORM search_delete_doc('profiles', OLD.id);
            ELSE
                IF NOT NEW.is_active THEN
                    -- Suspension: person doc + content docs vanish at once.
                    PERFORM search_delete_doc('profiles', NEW.id);
                    DELETE FROM {_TABLE} WHERE author_id = NEW.id;
                ELSE
                    -- Restore (or username change): re-index everything this
                    -- member owns so search honestly regains their content.
                    FOR v_guide IN SELECT id FROM guides_guide
                                    WHERE created_by_id = NEW.id
                    LOOP PERFORM search_upsert_guide(v_guide); END LOOP;
                    PERFORM search_upsert_person(NEW.id);
                    FOR v_item IN SELECT id FROM posts_post
                                   WHERE author_id = NEW.id
                    LOOP PERFORM search_upsert_post(v_item); END LOOP;
                    FOR v_item IN SELECT id FROM qa_question
                                   WHERE asked_by_id = NEW.id
                    LOOP PERFORM search_upsert_question(v_item); END LOOP;
                END IF;
            END IF;

        ELSE
            NULL;  -- unknown table — never invent data
    END CASE;
    RETURN NULL;  -- AFTER trigger: return value is ignored
END
$$;

-- ---------------------------------------------------------------- triggers --

CREATE TRIGGER search_guides_guide_aidu
    AFTER INSERT OR UPDATE OF status, created_by_id ON guides_guide
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_guides_guide_ad
    AFTER DELETE ON guides_guide
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_guides_revision_aiud
    AFTER INSERT OR UPDATE ON guides_guiderevision
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_guides_revision_ad
    AFTER DELETE ON guides_guiderevision
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_guides_step_aiud
    AFTER INSERT OR UPDATE ON guides_guidestep
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_guides_step_ad
    AFTER DELETE ON guides_guidestep
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_posts_post_aiud
    AFTER INSERT OR UPDATE ON posts_post
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_posts_post_ad
    AFTER DELETE ON posts_post
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_qa_question_aiud
    AFTER INSERT OR UPDATE ON qa_question
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_qa_question_ad
    AFTER DELETE ON qa_question
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_qa_answer_aiud
    AFTER INSERT OR UPDATE ON qa_answer
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_qa_answer_ad
    AFTER DELETE ON qa_answer
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_accounts_user_aiud
    AFTER INSERT OR UPDATE ON accounts_user
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_accounts_user_ad
    AFTER DELETE ON accounts_user
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_profiles_profile_aiud
    AFTER INSERT OR UPDATE ON profiles_profile
    FOR EACH ROW EXECUTE FUNCTION search_refresh();

CREATE TRIGGER search_profiles_profile_ad
    AFTER DELETE ON profiles_profile
    FOR EACH ROW EXECUTE FUNCTION search_refresh();
"""

TRIGGER_REVERSE_SQL = f"""
DROP TRIGGER IF EXISTS search_guides_guide_aidu ON guides_guide;
DROP TRIGGER IF EXISTS search_guides_guide_ad ON guides_guide;
DROP TRIGGER IF EXISTS search_guides_revision_aiud ON guides_guiderevision;
DROP TRIGGER IF EXISTS search_guides_revision_ad ON guides_guiderevision;
DROP TRIGGER IF EXISTS search_guides_step_aiud ON guides_guidestep;
DROP TRIGGER IF EXISTS search_guides_step_ad ON guides_guidestep;
DROP TRIGGER IF EXISTS search_posts_post_aiud ON posts_post;
DROP TRIGGER IF EXISTS search_posts_post_ad ON posts_post;
DROP TRIGGER IF EXISTS search_qa_question_aiud ON qa_question;
DROP TRIGGER IF EXISTS search_qa_question_ad ON qa_question;
DROP TRIGGER IF EXISTS search_qa_answer_aiud ON qa_answer;
DROP TRIGGER IF EXISTS search_qa_answer_ad ON qa_answer;
DROP TRIGGER IF EXISTS search_accounts_user_aiud ON accounts_user;
DROP TRIGGER IF EXISTS search_accounts_user_ad ON accounts_user;
DROP TRIGGER IF EXISTS search_profiles_profile_aiud ON profiles_profile;
DROP TRIGGER IF EXISTS search_profiles_profile_ad ON profiles_profile;
DROP INDEX IF EXISTS search_document_vector_gin;
DROP INDEX IF EXISTS search_document_body_trgm;
DROP INDEX IF EXISTS search_document_title_en_trgm;
DROP INDEX IF EXISTS search_document_title_sw_trgm;
DROP FUNCTION IF EXISTS search_refresh();
DROP FUNCTION IF EXISTS search_upsert_person(bigint);
DROP FUNCTION IF EXISTS search_upsert_question(bigint);
DROP FUNCTION IF EXISTS search_upsert_post(bigint);
DROP FUNCTION IF EXISTS search_upsert_guide(bigint);
DROP FUNCTION IF EXISTS search_delete_doc(text, bigint);
DROP FUNCTION IF EXISTS search_author_blocked(bigint);
DROP FUNCTION IF EXISTS search_set_doc(text, text, text, bigint,
    text, text, text, tsvector, bigint, text, text, timestamptz);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_loginattempt_verificationcode_user_email_verified_at_and_more"),
        ("guides", "0003_offlinesavedguide"),
        ("posts", "0001_initial"),
        ("profiles", "0001_initial"),
        ("qa", "0001_initial"),
        ("search", "0001_initial"),
    ]

    operations = [
        # WHY here: the trigram index and similarity() calls need pg_trgm at
        # run time; it is a TRUSTED extension on PG13+, so the mtaani role
        # can create it without superuser rights (verified on the test DB).
        CreateExtension("pg_trgm"),
        migrations.RunSQL(sql=TRIGGER_SQL, reverse_sql=TRIGGER_REVERSE_SQL),
    ]
