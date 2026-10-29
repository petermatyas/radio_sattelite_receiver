"""Közös teszt-beállítások: a tesztek ne függjenek a gépen telepített külső programoktól (SatDump, rtl-sdr)."""
import pytest


@pytest.fixture(autouse=True)
def no_system_tools(monkeypatch):
    """A PATH-ban és a szokásos helyeken nem találhatók külső programok; a tesztek a saját (hamis) programjukat adják meg."""
    import satrx.tools as tools

    real_which = tools.shutil.which
    monkeypatch.setattr(tools.shutil, "which",
                        lambda name, *a, **k: None if str(name).split(".")[0] in ("satdump", "rtl_sdr", "rtl_fm", "rtl_test")
                        else real_which(name, *a, **k))
    monkeypatch.setattr(tools, "_default_locations", lambda: [])
    monkeypatch.setattr(tools, "_rtl_locations", lambda name: [])
