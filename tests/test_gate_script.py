import importlib.util, pathlib

spec = importlib.util.spec_from_file_location(
    "check_mr", pathlib.Path(__file__).parent.parent / "ci-template" / "check_mr.py")
check_mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_mr)

def test_feature_without_tests_fails():
    assert check_mr.classify(["src/api.py", "src/db.py", "src/models.py"],
                             ["feat: add billing"]) == "fail-needs-tests"

def test_feature_with_tests_passes():
    assert check_mr.classify(["src/api.py", "tests/test_api.py"],
                             ["feat: add billing"]) == "pass"

def test_non_feature_small_change_passes():
    assert check_mr.classify(["README.md"], ["docs: typo"]) == "pass"

def test_gate_skip_trailer_skips():
    assert check_mr.classify(["src/api.py"] * 5,
                             ["hotfix\n\nGate-Skip: prod incident"]) == "skip"

def test_three_source_files_is_feature_even_without_feat_prefix():
    assert check_mr.classify(["a.py", "b.py", "c.py"], ["update stuff"]) == "fail-needs-tests"
