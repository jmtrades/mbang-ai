"""A demo seeded by an older build (before some features existed) is re-seeded once, so new features never show
empty demo pages; a demo already seeded with today's features, and the real workspace, are left alone."""


def _count(t, table):
    with t.db() as c:
        return c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


def test_demo_seeded_by_an_older_build_is_reseeded_once(demo_tenant):
    from operator_ai import demo

    assert demo_tenant.get("demo_features") == demo.FEATURE_SEEDERS
    with demo_tenant.db() as c:  # what an older build left behind: the base seed, no academy, no feature list
        c.execute("DELETE FROM academy_lessons")
        c.execute("DELETE FROM settings WHERE key='demo_features'")
    assert _count(demo_tenant, "academy_lessons") == 0

    demo.ensure_seeded(demo_tenant)
    assert _count(demo_tenant, "academy_lessons") > 0
    assert demo_tenant.get("demo_features") == demo.FEATURE_SEEDERS
    with demo_tenant.db() as c:
        assert c.execute("SELECT 1 FROM audit WHERE actor='system:upgrade' AND kind='demo.reset'").fetchone()

    with demo_tenant.db() as c:
        before = c.execute("SELECT COUNT(*) n FROM audit WHERE kind='demo.reset'").fetchone()["n"]
    demo.ensure_seeded(demo_tenant)  # up to date now: no second reset
    with demo_tenant.db() as c:
        assert c.execute("SELECT COUNT(*) n FROM audit WHERE kind='demo.reset'").fetchone()["n"] == before


def test_real_workspace_is_never_reseeded(tenant):
    from operator_ai import demo

    tenant.set("seeded_at", 1.0)  # even if something marked it
    demo.ensure_seeded(tenant)
    assert tenant.get("demo_features") is None
    assert _count(tenant, "academy_lessons") == 0
