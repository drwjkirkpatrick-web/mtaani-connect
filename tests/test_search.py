"""Tests for Prompt 14 — cross-entity search (apps.search).

Contract points asserted here:
- bilingual matching (en FTS + sw 'simple' vectors)
- trigram typo tolerance: "sukuma wiki" finds "sukumawiki"
- grouped results with honest counts + links to each entity's own list
- the search page stays under the 50 KB budget with a 20-result fixture
- empty-state copy in BOTH languages
- the vector is a MAINTAINED column (trigger refresh on content edits)
- REMOVED content / suspended authors never appear; restore re-indexes
"""
import re

from django.test import TestCase
from django.urls import reverse

from apps.core.testing import assert_under_budget
from apps.core.types import ContentStatus
from apps.guides.models import Guide, GuideRevision, GuideStep
from apps.posts.models import Post
from apps.profiles.models import Profile
from apps.qa.models import Answer, Question
from apps.search.models import SearchDocument

PUBLISHED = ContentStatus.PUBLISHED.value
REMOVED = ContentStatus.REMOVED.value


def _member(username="wanjiku"):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="test-pass-123"
    )


def _guide(owner, *, title_en, title_sw="", steps=("Boil the water first.",),
           status=PUBLISHED, revision_status=PUBLISHED):
    guide = Guide.objects.create(
        title_en=title_en, title_sw=title_sw, category="cooking",
        skill_level="beginner", created_by=owner, status=status,
    )
    revision = GuideRevision.objects.create(
        guide=guide, version=1, title_en=title_en, title_sw=title_sw,
        safety_category="NONE", safety_warning="",
        status=revision_status, published_at=None if revision_status != PUBLISHED
        else revision_now(),
    )
    for position, text in enumerate(steps, start=1):
        GuideStep.objects.create(revision=revision, position=position, text=text)
    return guide


def revision_now():
    from django.utils import timezone

    return timezone.now()


class SearchIndexMaintenanceTests(TestCase):
    """The maintained-column contract: triggers, not inline computation."""

    def test_guide_document_indexed_from_latest_published_revision(self):
        user = _member()
        guide = _guide(user, title_en="How to cook sukuma wiki", steps=("Wash the leaves.",))
        doc = SearchDocument.objects.get(app_label="guides")
        self.assertEqual(doc.kind, "guide")
        # NOTE: sequence-based pk — never hardcode "1" (sequences don't reset
        # on rollback in a shared suite; earlier tests advance the counter).
        self.assertEqual(doc.object_pk, str(guide.pk))
        self.assertEqual(doc.title_en, "How to cook sukuma wiki")
        self.assertIn("Wash the leaves.", doc.body)
        self.assertEqual(doc.url, f"/guides/{guide.pk}/")

    def test_trigger_refresh_after_question_edit(self):
        user = _member()
        question = Question.objects.create(
            title_en="How do I fix a tyre?", asked_by=user, status=PUBLISHED,
        )
        doc = SearchDocument.objects.get(app_label="qa", object_pk=str(question.pk))
        old_vector_matches_old_text = self._vector_matches(doc, "tyre")
        self.assertTrue(old_vector_matches_old_text)

        # WHY not a helper: this ORM save IS the proof — nothing else runs,
        # yet the MAINTAINED vector and body must follow the edit.
        question.title_en = "Which ferry goes to Likoni?"
        question.body = "Which ferry goes to Likoni early morning"
        question.save(update_fields=["title_en", "body", "updated_at"])

        doc.refresh_from_db()
        self.assertIn("Likoni", doc.body)
        self.assertFalse("fix a tyre" in doc.body)
        self.assertTrue(self._vector_matches(doc, "ferry"))
        self.assertFalse(self._vector_matches(doc, "tyre"))

    def _vector_matches(self, doc, term):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT vector @@ websearch_to_tsquery('simple', %s) "
                "FROM search_searchdocument WHERE id = %s",
                [term, doc.pk],
            )
            return cursor.fetchone()[0]

    def test_unpublished_or_removed_content_never_indexed(self):
        user = _member()
        draft = _guide(user, title_en="Draft only guide", revision_status="DRAFT")
        self.assertFalse(SearchDocument.objects.filter(app_label="guides").exists())

        live = _guide(user, title_en="Live guide")
        self.assertTrue(
            SearchDocument.objects.filter(app_label="guides", object_pk=str(live.pk)).exists()
        )
        live.status = REMOVED  # moderator soft delete
        live.save(update_fields=["status", "updated_at"])
        self.assertFalse(SearchDocument.objects.filter(app_label="guides").exists())

        post = Post.objects.create(author=user, text="Post about draft", status=PUBLISHED)
        self.assertTrue(
            SearchDocument.objects.filter(app_label="posts", object_pk=str(post.pk)).exists()
        )
        post.status = REMOVED
        post.save(update_fields=["status"])
        self.assertFalse(
            SearchDocument.objects.filter(app_label="posts", object_pk=str(post.pk)).exists()
        )

        question = Question.objects.create(
            title_en="Question draft", asked_by=user, status=PUBLISHED,
        )
        question.status = REMOVED
        question.save(update_fields=["status"])
        self.assertFalse(
            SearchDocument.objects.filter(app_label="qa", object_pk=str(question.pk)).exists()
        )
        del draft  # draft guide contributes no document at all (asserted above)

    def test_suspended_author_content_hidden_then_restored(self):
        user = _member()
        post = Post.objects.create(author=user, text="Suspended word kazi", status=PUBLISHED)
        self.assertTrue(
            SearchDocument.objects.filter(app_label="posts", object_pk=str(post.pk)).exists()
        )
        user.is_active = False  # what moderation's confirmed suspension sets
        user.save(update_fields=["is_active"])
        self.assertFalse(
            SearchDocument.objects.filter(author=user).exists(),
            "suspended author's content must leave search",
        )
        user.is_active = True
        user.save(update_fields=["is_active"])
        self.assertTrue(
            SearchDocument.objects.filter(app_label="posts", object_pk=str(post.pk)).exists(),
            "restored account content must honestly return",
        )

    def test_answer_write_refreshes_parent_question_document(self):
        user = _member()
        question = Question.objects.create(
            title_en="Laundry tips?", asked_by=user, status=PUBLISHED,
        )
        answer = Answer.objects.create(
            question=question, author=user, body="Fua nguo kwa baridi asubuhi.",
        )
        doc = SearchDocument.objects.get(app_label="qa", object_pk=str(question.pk))
        self.assertIn(answer.body, doc.body)
        self.assertTrue(self._vector_matches(doc, "fua"))

    def test_people_index_only_public_fields(self):
        user = _member("njeri")  # email njeri@example.com is NEVER indexed
        user.phone = "+254700111222"
        user.save()
        Profile.objects.create(
            user=user, display_name="Mama Njeri", bio="Vitambulisho vya duka", ward="Kibera",
        )
        doc = SearchDocument.objects.get(app_label="profiles", object_pk=str(user.pk))
        self.assertEqual(doc.kind, "person")
        self.assertEqual(doc.title_en, "Mama Njeri")
        self.assertIn("Kibera", doc.ward)
        # KDPA: no phone numbers or e-mail addresses in searchable bodies.
        self.assertNotIn("+254700111222", doc.body)
        self.assertNotIn("njeri@example.com", doc.body)

        # A member making their bio private loses it from search on save.
        profile = Profile.objects.get(user=user)
        profile.field_visibility = {"bio": "private"}
        profile.save(update_fields=["field_visibility"])
        doc.refresh_from_db()
        self.assertNotIn("Vitambulisho vya duka", doc.body)
        self.assertIn("Mama Njeri", doc.body)  # public field remains


class SearchViewTests(TestCase):
    """HTTP contract: grouping, counts, links, budget, bilingual empty state."""

    def _get_search(self, query, **extra):
        return self.client.get(reverse("search:search"), {"q": query} if query else {}, **extra)

    def test_bilingual_matching_en_and_sw(self):
        user = _member()
        _guide(user, title_en="How to cook sukuma wiki", steps=("Wash the leaves first.",))
        Question.objects.create(
            title_en="Hand washing clothes",
            title_sw="Kufua nguo kwa mkono",
            asked_by=user, status=PUBLISHED,
        )
        # English query → English full-text path.
        response = self._get_search("leaves")
        self.assertContains(response, "How to cook sukuma wiki")

        # Kiswahili query → 'simple' vector path over the sw field.
        response = self._get_search("kufua")
        self.assertContains(response, "Kufua nguo kwa mkono")
        self.assertNotContains(response, "sukuma wiki")

    def test_trigram_finds_misspelled_words(self):
        user = _member()
        _guide(user, title_en="Making sukumawiki stew", steps=("Chop it fresh.",))
        # No full-text match ('sukumawiki' is one token), so only the trigram
        # path can surface this guide when the query is "sukuma wiki".
        response = self._get_search("sukuma wiki")
        self.assertContains(response, "Making sukumawiki stew")

    def test_grouped_results_with_counts_and_links(self):
        user = _member()
        _guide(user, title_en="Kazi ya umeme nyumbani", steps=("Kazi huanza hapa.",))
        _guide(user, title_en="Kazi ya maji", steps=("Kazi haiishi.",))
        for i in range(3):
            Post.objects.create(author=user, text=f"Kazi ya boda {i} leo", status=PUBLISHED)
        Question.objects.create(
            title_en="Kazi kwa wiki hii", asked_by=user, status=PUBLISHED,
        )
        Profile.objects.create(user=user, display_name="Kazi Wanjiku", ward="Kibera")

        response = self._get_search("kazi")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2 results")
        self.assertContains(response, "3 results")
        self.assertContains(response, "1 result")
        for href in ('/guides/?q=kazi', '/posts/', '/qa/?q=kazi'):
            self.assertContains(response, href)
        # People group links items to their profile page (no people-list
        # route exists yet — the header link is honestly absent).
        self.assertContains(response, "/profiles/u/wanjiku/")

    def test_rank_order_is_deterministic(self):
        user = _member()
        title_match = Question.objects.create(
            title_en="Fixing a phone", asked_by=user, status=PUBLISHED,
        )
        body_match = Question.objects.create(
            title_en="Road works notice",
            body="Fixing a phone in this ward happens on market days.",
            asked_by=user, status=PUBLISHED,
        )
        self.assertNotEqual(title_match.pk, body_match.pk)
        response = self._get_search("fixing a phone")
        titles = re.findall(r"search-item[^>]*>\s*<a[^>]*>([^<]+)</a>", response.content.decode())
        question_titles = [t for t in titles if t in ("Fixing a phone", "Road works notice")]
        # Weight-A title hit outranks the weight-B body hit — deterministic.
        self.assertEqual(question_titles[0], "Fixing a phone")

    def test_page_budget_with_20_result_fixture(self):
        user = _member()
        for i in range(20):
            Post.objects.create(author=user, text=f"Matatu route {i} Nairobi notes",
                                status=PUBLISHED)
        response = self._get_search("matatu")
        html = response.content.decode()
        self.assertEqual(len(re.findall(r'class="search-item"', html)), 20)
        assert_under_budget(html, kb=50)

    def test_empty_state_copy_in_english(self):
        response = self._get_search("zzznotathing")
        self.assertContains(response, "No results for “zzznotathing”.")
        self.assertContains(response, "Try different words or check your spelling.")

    def test_empty_state_prompts_without_query_in_english(self):
        response = self.client.get(reverse("search:search"))
        self.assertContains(response, "Search guides, posts, questions and people.")
        self.assertContains(response, "Type a few words and press Search.")

    def test_empty_state_copy_in_swahili(self):
        # The real language round-trip: the django_language cookie the
        # /lang/<code>/ toggle sets (LocaleMiddleware reads it).
        self.client.cookies["django_language"] = "sw"
        response = self._get_search("zzznotathing")
        text = response.content.decode()
        self.assertIn("Hakuna matokeo kwa “zzznotathing”.", text)
        self.assertIn("Jaribu maneno mengine au angalia tahajia yako.", text)

        response = self.client.get(reverse("search:search"))
        text = response.content.decode()
        self.assertIn("Tafuta miongozo, machapisho, maswali na watu.", text)
        self.assertIn("Andika maneno machache kisha bonyeza Tafuta.", text)
