from tsbench import guards
from tsbench.guards import check_guards, idle_seconds, on_battery, running_call_apps


def fake(**outputs):
    """A runner that answers by command name: fake(pmset=..., ioreg=..., ps=...)."""
    def runner(argv, timeout=None):
        out = outputs.get(argv[0])
        return (127, "", "not found") if out is None else (0, out, "")
    return runner


BATT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t71%; discharging\n"
AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged\n"


def hid(seconds):
    return f'    | |   "HIDIdleTime" = {int(seconds * 1e9)}\n'


def test_battery_detected_on_macos():
    assert on_battery(fake(pmset=BATT), platform="darwin") is True
    assert on_battery(fake(pmset=AC), platform="darwin") is False
    assert on_battery(fake(), platform="darwin") is None            # pmset unavailable


def test_battery_on_linux_reads_sysfs(tmp_path):
    bat = tmp_path / "BAT0"
    bat.mkdir()
    (bat / "status").write_text("Discharging\n")
    assert on_battery(platform="linux", power_dir=str(tmp_path)) is True
    (bat / "status").write_text("Charging\n")
    assert on_battery(platform="linux", power_dir=str(tmp_path)) is False


def test_no_battery_means_mains(tmp_path):
    assert on_battery(platform="linux", power_dir=str(tmp_path)) is None


def test_idle_time_is_parsed_in_seconds():
    assert idle_seconds(fake(ioreg=hid(125.5)), platform="darwin") == 125.5
    assert idle_seconds(fake(ioreg="nothing useful"), platform="darwin") is None
    assert idle_seconds(fake(ioreg=hid(5)), platform="linux") is None   # unknown off macOS


def test_call_apps_match_process_names_case_insensitively():
    ps = "launchd\nCptHost\nFinder\nMSTeams\n"
    assert running_call_apps(["cpthost", "facetime"], fake(ps=ps), "darwin") == ["cpthost"]
    assert running_call_apps(["msteams"], fake(ps=ps), "darwin") == ["msteams"]
    assert running_call_apps([], fake(ps=ps), "darwin") == []


def test_merely_open_apps_are_not_treated_as_calls_by_default():
    ps = "zoom.us\nMSTeams\nMicrosoft Teams\nWebex\n"
    assert running_call_apps(guards.DEFAULT_CALL_PROCESSES, fake(ps=ps), "darwin") == []


def test_all_clear_returns_none():
    r = fake(pmset=AC, ioreg=hid(300), ps="launchd\n")
    assert check_guards(runner=r, platform="darwin") is None


def test_each_guard_can_block_and_says_why():
    assert "battery" in check_guards(runner=fake(pmset=BATT, ioreg=hid(300), ps=""),
                                     platform="darwin")
    assert "in use" in check_guards(runner=fake(pmset=AC, ioreg=hid(3), ps=""),
                                    platform="darwin")
    assert "call" in check_guards(runner=fake(pmset=AC, ioreg=hid(300), ps="CptHost\n"),
                                  platform="darwin")


def test_guards_can_be_relaxed():
    r = fake(pmset=BATT, ioreg=hid(3), ps="CptHost\n")
    assert check_guards(min_idle_s=0, call_apps=[], battery=False, runner=r,
                        platform="darwin") is None


def test_unknown_answers_never_block():
    assert check_guards(runner=fake(), platform="darwin") is None
