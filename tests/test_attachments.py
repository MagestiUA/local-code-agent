"""Офлайн-тест agent/attachments.py (диск + метадані).
Запуск: .venv\\Scripts\\python.exe -m tests.test_attachments
"""
import shutil

from agent.attachments import (
    classify, process, session_dir, save, remove, clear, rename_session,
    list_saved, attachments_note, CHAR_BUDGET, MAX_FILE_SIZE,
)

SID = "_test_session_xyz"
SID2 = "_test_session_xyz2"


def main():
    # classify
    assert classify("main.py", b"print('hello')") == "text"
    assert classify("data.bin", bytes(range(256))) == "binary"
    assert classify("README.md", b"# Hello") == "text"
    assert classify("image.png", b"\x89PNG\r\n") == "binary"

    # process: нормальний текст
    r = process("foo.py", b"x = 1\n")
    assert r["error"] == ""
    assert r["_content"] == "x = 1\n"
    assert not r["truncated"]

    # process: завеликий / бінарний → error, без _content
    assert process("big.txt", b"x" * (MAX_FILE_SIZE + 1))["error"] != ""
    r3 = process("img.png", b"\x89PNG\r\n\x1a\n")
    assert r3["error"] != ""
    assert "_content" not in r3

    # process: обрізання до CHAR_BUDGET
    r4 = process("long.py", ("a" * (CHAR_BUDGET + 500)).encode())
    assert r4["error"] == "" and r4["truncated"]
    assert len(r4["_content"]) == CHAR_BUDGET

    # ── Диск: save / list_saved / remove / clear ──
    clear(SID); clear(SID2)
    try:
        p = save(SID, "main.py", "print(1)\n")
        assert p.is_file() and p.read_text(encoding="utf-8") == "print(1)\n"
        assert p.parent == session_dir(SID)
        save(SID, "utils.py", "x = 2\n")

        meta = list_saved(SID)
        names = {m["name"] for m in meta}
        assert names == {"main.py", "utils.py"}
        assert all(m["error"] == "" for m in meta)

        # traversal-захист: лише базове ім'я
        ev = save(SID, "../evil.py", "bad")
        assert ev.parent == session_dir(SID) and ev.name == "evil.py"
        remove(SID, "evil.py")

        # attachments_note: імена файлів + інструкція read_attachment, без вмісту
        note = attachments_note(SID, meta)
        assert "main.py" in note and "utils.py" in note
        assert "read_attachment" in note         # стеримо модель на правильний тул
        assert "print(1)" not in note            # вмісту немає
        assert attachments_note(SID, []) == ""
        # error-файли не потрапляють у note
        assert attachments_note(SID, [{"name": "x", "error": "бінарний"}]) == ""

        # remove одного файлу
        remove(SID, "utils.py")
        assert {m["name"] for m in list_saved(SID)} == {"main.py"}

        # rename_session: міграція _tmp → реальна сесія
        rename_session(SID, SID2)
        assert not session_dir(SID).exists()
        assert {m["name"] for m in list_saved(SID2)} == {"main.py"}

        # list_saved на відсутній теці → []
        assert list_saved("nope_nonexistent") == []

        # read_attachment: нечіткий пошук за іменем у теці вкладень
        from agent.toolkit import ToolContext, h_read_attachment
        ctx = ToolContext(root=session_dir(SID2), attachments_dir=session_dir(SID2))
        assert "print(1)" in h_read_attachment({"name": "main.py"}, ctx)   # exact
        assert "print(1)" in h_read_attachment({"name": "MAIN.PY"}, ctx)   # case-insensitive
        assert "print(1)" in h_read_attachment({"name": "main"}, ctx)      # підрядок
        # повний шлях теж зводиться до базового імені
        assert "print(1)" in h_read_attachment(
            {"name": str(session_dir(SID2) / "main.py")}, ctx)
        assert "не знайдено" in h_read_attachment({"name": "zzz.py"}, ctx)
        # без attachments_dir → зрозуміле повідомлення
        assert "немає" in h_read_attachment({"name": "x"}, ToolContext(root=session_dir(SID2)))
    finally:
        clear(SID); clear(SID2)

    assert not session_dir(SID).exists()

    print("OK: attachments — classify, process, save/list_saved/remove/clear, "
          "rename_session, attachments_note, read_attachment (нечіткий пошук), "
          "traversal-захист")


if __name__ == "__main__":
    main()
