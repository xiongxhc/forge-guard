from forgeguard.state import State

def test_roundtrip_and_atomic_write(tmp_path):
    p = str(tmp_path / "deep" / "state.json")
    st = State.load(p)
    assert st.get_tip(1, "main") is None
    st.set_tip(1, "main", "abc")
    st.set_cursor("mr_updated_after", "2026-08-07T00:00:00Z")
    st.save(p)
    st2 = State.load(p)
    assert st2.get_tip(1, "main") == "abc"
    assert st2.get_cursor("mr_updated_after") == "2026-08-07T00:00:00Z"
    assert not list(tmp_path.glob("**/*.tmp"))

def test_flag_once():
    st = State.load("/nonexistent/x.json")
    assert st.flag_once("usermap:alice") is True
    assert st.flag_once("usermap:alice") is False

def test_clear_flags_removes_only_matching_limit_episodes():
    st = State()
    st.flag_once("limit:Claude:five_hour:unknown")
    st.flag_once("limit:Claude:five_hour:1788")
    st.flag_once("limit:Codex:usage:unknown")
    st.clear_flags("limit:Claude:", ":unknown")
    assert not st.flagged("limit:Claude:five_hour:unknown")
    assert st.flagged("limit:Claude:five_hour:1788")
    assert st.flagged("limit:Codex:usage:unknown")

def test_save_bare_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    st = State()
    st.set_tip(2, "dev", "xyz")
    st.save("flat.json")
    st2 = State.load("flat.json")
    assert st2.get_tip(2, "dev") == "xyz"

def test_load_corrupt_json(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("{not json")
    st = State.load(str(p))
    assert st.get_tip(1, "main") is None
