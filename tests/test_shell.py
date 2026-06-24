"""Тест allow-list для run_shell (без Ollama).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_shell
"""
from agent.tools import run_shell


def main() -> None:
    # 1) Дозволена команда виконується.
    r = run_shell("python --version")
    assert r.allowed, "python заблоковано помилково"
    assert r.returncode == 0, r.stderr
    assert "Python" in (r.stdout + r.stderr)

    # 2) Деструктивна команда поза allow-list — блокується БЕЗ виконання.
    blocked = run_shell("del important.txt")
    assert not blocked.allowed, "деструктивну команду НЕ заблоковано"
    assert blocked.returncode == -1

    # 3) Ланцюжок не виконується (shell=False): друга частина не спрацьовує.
    chain = run_shell('python -c "print(1)"; echo HACKED')
    assert chain.allowed  # префікс python дозволений
    assert "HACKED" not in chain.stdout, "ланцюжок виконався — діра в безпеці!"

    # 4) Обгорткові лапки знімаються з токенів (commit-повідомлення / лапкові аргументи),
    #    щоб `-m "msg"` давало чисте 'msg'. Перевірка через argv[1].
    q = run_shell('python -c "import sys;print(sys.argv[1])" "hello world"')
    assert q.allowed and q.returncode == 0, q.stderr
    assert q.stdout.strip() == "hello world", repr(q.stdout)  # без лапок навколо

    print("OK: allow-list, захист від ланцюжків і зняття лапок працюють")
    print(f"  python --version -> rc={r.returncode}")
    print(f"  'del important.txt' -> allowed={blocked.allowed} (заблоковано)")
    print(f"  ланцюжок -> 'HACKED' у виводі: {'HACKED' in chain.stdout}")


if __name__ == "__main__":
    main()
