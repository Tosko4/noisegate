from __future__ import annotations

import sys
from typing import NoReturn

import noisegate.wrap as wrap_module
from noisegate.engine import NoisegateOptions
from noisegate.wrap import run_wrapped_command


def test_wrapped_command_fails_open_when_reducer_raises(monkeypatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("boom")

    monkeypatch.setattr(wrap_module, "reduce_text", boom)

    result = run_wrapped_command(
        [sys.executable, "-c", "print('raw output')"],
        options=NoisegateOptions(max_chars=1),
    )

    assert result.exit_code == 0
    assert result.text == "raw output\n"
    assert result.metadata["mode"] == "fail_open"
