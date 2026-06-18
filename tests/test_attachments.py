"""Офлайн-тест agent/attachments.py.
Запуск: .venv\\Scripts\\python.exe -m tests.test_attachments
"""
from agent.attachments import (
    classify, process, format_single, find_for_step, attachment_summary,
    CHAR_BUDGET, MAX_FILE_SIZE,
)


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

    # process: завеликий
    big = b"x" * (MAX_FILE_SIZE + 1)
    r2 = process("big.txt", big)
    assert r2["error"] != ""

    # process: бінарний
    r3 = process("img.png", b"\x89PNG\r\n\x1a\n")
    assert r3["error"] != ""

    # process: обрізання до CHAR_BUDGET
    long_content = ("a" * (CHAR_BUDGET + 500)).encode()
    r4 = process("long.py", long_content)
    assert r4["error"] == ""
    assert r4["truncated"]
    assert len(r4["_content"]) == CHAR_BUDGET

    # format_single
    out = format_single("a.py", "print(1)", truncated=False)
    assert "a.py" in out
    assert "print(1)" in out
    assert "[обрізано]" not in out

    out_trunc = format_single("a.py", "x", truncated=True)
    assert "[обрізано]" in out_trunc

    # find_for_step
    meta = [
        {"name": "main.py", "size": 100, "truncated": False, "error": ""},
        {"name": "utils.py", "size": 200, "truncated": False, "error": ""},
        {"name": "bad.bin", "size": 50, "truncated": False, "error": "бінарний"},
    ]
    assert find_for_step("Проаналізуй main.py і знайди баги", meta) == "main.py"
    assert find_for_step("Відрефактори utils.py", meta) == "utils.py"
    assert find_for_step("bad.bin треба виправити", meta) is None  # error → пропуск
    assert find_for_step("Загальне завдання без файлів", meta) is None

    # attachment_summary
    summary = attachment_summary(meta)
    assert "main.py" in summary
    assert "utils.py" in summary
    assert "бінарний" in summary   # помилка відображається
    assert "Прикріплені файли:" in summary

    assert attachment_summary([]) == ""

    print("OK: attachments — classify, process (обрізання, бінар, розмір), "
          "format_single, find_for_step, attachment_summary")


if __name__ == "__main__":
    main()
