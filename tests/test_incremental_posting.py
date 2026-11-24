"""Incremental whole-box CSV replacement semantics.

These tests cover the optimisation where re-posting a corrected box only
rechecks added/removed/changed archive ids while leaving every other
difference row intact, plus the atomic posting / stale / failure behaviour.
"""

import pytest

from archive_reconcile.canonical import Item
from archive_reconcile.store import Store
from tests.helpers import (
    board,
    make_batch,
    manifest_csv,
    replace_box,
    submit_detail,
)


def B1_ITEM(archive_id, title=None, retention="永久"):
    return Item("B1", archive_id, title or f"题名 {archive_id}", retention)


@pytest.fixture()
def mixed_batch(client, store):
    """B1 old detail vs a new version with missing/extra/changed rows."""

    old = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复", "30年"),
        B1_ITEM("A-003", "会议纪要", "10年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
    ]
    new = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复（更正）", "30年"),  # changed
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "新多出来的", "10年"),  # extra
        # A-003 missing
    ]
    batch_id = make_batch(
        client, {"B1": new}, manifest=manifest_csv({"B1": old})
    )
    version_id = board(client, store, batch_id).get("B1").new_version_id
    assert submit_detail(client, batch_id, "B1", old, version_id).status_code == 302
    recon = store.get_reconciliation(batch_id, "B1")
    rows = {d["archive_id"]: d for d in store.get_differences(recon["id"])}
    assert set(rows) == {"A-002", "A-003", "A-009"}
    return {
        "batch_id": batch_id,
        "old": old,
        "new": new,
        "version_id": version_id,
        "recon_id": recon["id"],
        "rows": rows,
    }


def _post_replace(store, batch_id, items, expected_version_id):
    return store.import_snapshot(
        batch_id,
        {"B1": items},
        source="box-replacement",
        expected_versions={"B1": expected_version_id},
    )


def test_untouched_difference_rows_keep_their_identity(client, store, mixed_batch):
    batch_id = mixed_batch["batch_id"]
    current_id = store.get_current_version(batch_id, "B1").id

    # Only correct the extra record's title; A-002 and A-003 are untouched.
    corrected = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复（更正）", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "新多出来的（再更正）", "10年"),
    ]
    _post_replace(store, batch_id, corrected, current_id)

    recon = store.get_reconciliation(batch_id, "B1")
    rows = {d["archive_id"]: d for d in store.get_differences(recon["id"])}

    # The reconciliation row is patched in place when differences survive.
    assert recon["id"] == mixed_batch["recon_id"]
    # Untouched rows are literally the same rows.
    assert rows["A-002"]["id"] == mixed_batch["rows"]["A-002"]["id"]
    assert rows["A-003"]["id"] == mixed_batch["rows"]["A-003"]["id"]
    # The corrected row keeps its id too and shows the new value.
    assert rows["A-009"]["id"] == mixed_batch["rows"]["A-009"]["id"]
    assert rows["A-009"]["new_title"] == "新多出来的（再更正）"


def test_only_changed_ids_are_queried_in_old_detail_and_differences(
    monkeypatch, store, mixed_batch
):
    batch_id = mixed_batch["batch_id"]
    current_id = store.get_current_version(batch_id, "B1").id

    original_detail_loader = Store._target_detail_items
    detail_calls = []

    def detail_spy(conn, detail_id, archive_ids, box_no):
        ids = list(archive_ids)
        detail_calls.append(ids)
        return original_detail_loader(conn, detail_id, ids, box_no)

    original_diff_loader = Store._target_difference_rows
    diff_calls = []

    def diff_spy(conn, reconciliation_id, archive_ids):
        ids = list(archive_ids)
        diff_calls.append(ids)
        return original_diff_loader(conn, reconciliation_id, ids)

    monkeypatch.setattr(Store, "_target_detail_items", staticmethod(detail_spy))
    monkeypatch.setattr(Store, "_target_difference_rows", staticmethod(diff_spy))

    corrected = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复（更正）", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "只改这一条", "10年"),
    ]
    _post_replace(store, batch_id, corrected, current_id)

    # Each scoped loader is used once, and only for the corrected archive id.
    assert detail_calls == [["A-009"]]
    assert diff_calls == [["A-009"]]


def test_replacement_does_not_full_recompare_or_list_all_differences(
    monkeypatch, store, mixed_batch
):
    import archive_reconcile.store as store_module

    batch_id = mixed_batch["batch_id"]
    current_id = store.get_current_version(batch_id, "B1").id

    compare_calls = []
    original_compare = store_module.compare_box

    def spy_compare(*args, **kwargs):
        compare_calls.append(args)
        return original_compare(*args, **kwargs)

    monkeypatch.setattr(store_module, "compare_box", spy_compare)

    list_calls = []
    original_list = Store.get_differences

    def spy_list(self, reconciliation_id):
        list_calls.append(reconciliation_id)
        return original_list(self, reconciliation_id)

    monkeypatch.setattr(Store, "get_differences", spy_list)

    corrected = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复（更正）", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "新多出来的", "永久"),  # retention corrected
    ]
    _post_replace(store, batch_id, corrected, current_id)

    assert compare_calls == []
    assert list_calls == []


def test_disappeared_difference_is_withdrawn(store, mixed_batch):
    batch_id = mixed_batch["batch_id"]
    current_id = store.get_current_version(batch_id, "B1").id

    # Make A-002 exactly match the old record: its changed row must vanish.
    corrected = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "新多出来的", "10年"),
    ]
    _post_replace(store, batch_id, corrected, current_id)
    recon = store.get_reconciliation(batch_id, "B1")
    rows = {d["archive_id"]: d for d in store.get_differences(recon["id"])}
    assert "A-002" not in rows
    assert recon["summary"]["changed"] == 0
    assert recon["summary"]["extra"] == 1
    assert recon["summary"]["missing"] == 1  # A-003


def test_category_transitions_keep_the_same_row(store, mixed_batch):
    batch_id = mixed_batch["batch_id"]
    a3_id = mixed_batch["rows"]["A-003"]["id"]

    # Add A-003 back with a wrong title: missing -> changed (转类).
    current_id = store.get_current_version(batch_id, "B1").id
    items = store.load_version_items(current_id, "B1")
    items.append(B1_ITEM("A-003", "会议纪要（误）", "10年"))
    _post_replace(store, batch_id, items, current_id)

    recon = store.get_reconciliation(batch_id, "B1")
    rows = {d["archive_id"]: d for d in store.get_differences(recon["id"])}
    assert rows["A-003"]["kind"] == "changed"
    assert rows["A-003"]["id"] == a3_id
    assert rows["A-003"]["new_title"] == "会议纪要（误）"
    assert recon["summary"]["missing"] == 0
    assert recon["summary"]["changed"] == 2  # A-002 and A-003

    # Delete A-003 again: changed -> missing, same row.
    current_id = store.get_current_version(batch_id, "B1").id
    items = [i for i in store.load_version_items(current_id, "B1") if i.archive_id != "A-003"]
    _post_replace(store, batch_id, items, current_id)
    recon = store.get_reconciliation(batch_id, "B1")
    rows = {d["archive_id"]: d for d in store.get_differences(recon["id"])}
    assert rows["A-003"]["kind"] == "missing"
    assert rows["A-003"]["id"] == a3_id
    assert recon["summary"]["changed"] == 1  # only A-002 remains
    assert recon["summary"]["missing"] == 1


def test_extra_row_value_refreshed_with_unchanged_subtotal(client, store, mixed_batch):
    batch_id = mixed_batch["batch_id"]
    current_id = store.get_current_version(batch_id, "B1").id

    corrected = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复（更正）", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "订正后的多录题名", "10年"),
    ]
    response = replace_box(
        client, batch_id, "B1", corrected, current_id
    )
    assert response.status_code == 302

    view = board(client, store, batch_id).get("B1")
    # Subtotals did not move ...
    assert (view.missing, view.extra, view.changed) == (1, 1, 1)

    # ... yet the detail row shows the corrected value, on the HTML page.
    page = client.get(f"/batches/{batch_id}/boxes/B1").data.decode("utf-8")
    assert "订正后的多录题名" in page

    recon = store.get_reconciliation(batch_id, "B1")
    row = next(
        d for d in store.get_differences(recon["id"]) if d["archive_id"] == "A-009"
    )
    assert row["new_title"] == "订正后的多录题名"


def test_equivalent_replacement_keeps_version_and_confirmation(client, store, mixed_batch):
    batch_id = mixed_batch["batch_id"]
    version_id = mixed_batch["version_id"]

    client.post(
        f"/batches/{batch_id}/boxes/B1/finalize",
        data={"expected_version_id": str(version_id)},
    )
    assert board(client, store, batch_id).get("B1").finalized is True

    # Same content in a different row order: canonical-equivalent.
    equivalent = list(reversed(mixed_batch["new"]))
    response = replace_box(client, batch_id, "B1", equivalent, version_id)
    assert response.status_code == 302

    view = board(client, store, batch_id).get("B1")
    assert view.new_version_id == version_id
    assert view.finalized is True
    recon = store.get_reconciliation(batch_id, "B1")
    assert recon["finalized"] is not None


def test_stale_post_is_rejected_but_page_shows_latest_results(client, store, mixed_batch):
    batch_id = mixed_batch["batch_id"]
    stale_version_id = mixed_batch["version_id"]

    # First tab wins and changes A-009.
    first = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复（更正）", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "第一标签交的新值", "10年"),
    ]
    assert replace_box(client, batch_id, "B1", first, stale_version_id).status_code == 302

    # Second tab still posts against the original version id.
    second = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "冲突的改写", "永久"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "第二标签的旧基线", "10年"),
    ]
    response = replace_box(client, batch_id, "B1", second, stale_version_id)
    assert response.status_code == 409
    page = response.data.decode("utf-8")
    assert "内容在核对途中已变更" in page
    # The previously posted (winning) results are still rendered.
    assert "第一标签交的新值" in page
    assert "冲突的改写" not in page

    current = board(client, store, batch_id).get("B1")
    assert current.new_version == 2


def test_detail_verified_while_box_empty_then_first_upload_is_incremental(
    client, store
):
    old = [
        B1_ITEM("C-001", "丙一", "永久"),
        B1_ITEM("C-002", "丙二", "30年"),
        B1_ITEM("C-003", "丙三", "10年"),
    ]
    # Manifest only: B1 has no new-archive rows at batch creation.
    batch_id = make_batch(client, {}, manifest=manifest_csv({"B1": old}))
    assert submit_detail(client, batch_id, "B1", old, 0).status_code == 302
    recon = store.get_reconciliation(batch_id, "B1")
    assert recon["summary"]["missing"] == 3
    missing_ids = {
        d["archive_id"]: d["id"] for d in store.get_differences(recon["id"])
    }

    first_upload = [
        B1_ITEM("C-001", "丙一", "永久"),
        B1_ITEM("C-002", "丙二（订正）", "30年"),
        B1_ITEM("C-009", "新增多录", "永久"),
    ]
    _post_replace(store, batch_id, first_upload, 0)

    recon = store.get_reconciliation(batch_id, "B1")
    rows = {d["archive_id"]: d for d in store.get_differences(recon["id"])}
    assert {(a, d["kind"]) for a, d in rows.items()} == {
        ("C-002", "changed"),
        ("C-003", "missing"),
        ("C-009", "extra"),
    }
    # missing -> changed keeps the row identity; untouched missing survives.
    assert rows["C-002"]["id"] == missing_ids["C-002"]
    assert rows["C-003"]["id"] == missing_ids["C-003"]
    assert (recon["summary"]["missing"], recon["summary"]["extra"], recon["summary"]["changed"]) == (1, 1, 1)


def test_content_change_with_existing_differences_withdraws_confirmation(
    client, store, mixed_batch
):
    batch_id = mixed_batch["batch_id"]
    version_id = mixed_batch["version_id"]
    assert (
        client.post(
            f"/batches/{batch_id}/boxes/B1/finalize",
            data={"expected_version_id": str(version_id)},
        ).status_code
        == 302
    )
    assert board(client, store, batch_id).get("B1").finalized is True

    current_id = store.get_current_version(batch_id, "B1").id
    changed = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "关于乙的批复（再改）", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "新多出来的", "10年"),
    ]
    assert replace_box(client, batch_id, "B1", changed, current_id).status_code == 302
    view = board(client, store, batch_id).get("B1")
    assert view.finalized is False
    assert store.get_reconciliation(batch_id, "B1")["finalized_at"] is None


def test_unexpected_posting_failure_keeps_and_renders_old_results(
    monkeypatch, client, store, mixed_batch
):
    batch_id = mixed_batch["batch_id"]
    current_id = store.get_current_version(batch_id, "B1").id

    def boom(*args, **kwargs):
        raise RuntimeError("simulated posting failure")

    monkeypatch.setattr(Store, "_refresh_summary", boom)

    bad = [
        B1_ITEM("A-001", "关于甲的报告", "永久"),
        B1_ITEM("A-002", "触发入账", "30年"),
        B1_ITEM("A-004", "丁项材料", "永久"),
        B1_ITEM("A-009", "新多出来的", "10年"),
    ]
    response = replace_box(client, batch_id, "B1", bad, current_id)
    assert response.status_code == 500
    page = response.data.decode("utf-8")
    # Previously posted result remains visible in the failure response.
    assert "关于乙的批复（更正）" in page
    assert "本次换交入账失败" in page

    # The attempted version/difference writes were rolled back.
    view = board(client, store, batch_id).get("B1")
    assert view.new_version_id == current_id
    recon = store.get_reconciliation(batch_id, "B1")
    rows = {d["archive_id"]: d for d in store.get_differences(recon["id"])}
    assert rows["A-002"]["new_title"] == "关于乙的批复（更正）"
