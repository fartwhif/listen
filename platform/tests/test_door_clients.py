"""family.door_clients: counting the humans at a vault's noVNC door.

This count is the single input three lifecycle decisions share: the transport
daemon wakes the demand browser on it, vault-run stays up on it and tears down
the moment it drops to 0, and the watchdog only force-stops an idle browser when
it is 0. A wrong count is silent at the door either way - too high and a browser
never leaves (the owner's forbidden warm tab), too low and a click never wakes.

The real bug this pins: a human reaches websockify ONLY through the gateway, and
that relayed leg is owned by NO process the scanner can attribute (nginx runs as
another uid; `ss -t` on the live container showed no owner on it either). The
first implementation required an ESTABLISHED leg to be owned by nginx, so it read
EVERY real human as 0 and the door never woke. The fix counts every established
leg to the door port EXCEPT the sockets this family's own automation holds - the
keyboard driver (comm `python3`, a direct ws client of x11vnc) is subtracted;
the ownerless gateway leg is counted. That relies only on enumerating our own
uid's processes, which is what is actually reliable.

`_count_door_sockets` is pure so the discrimination needs no gateway, browser or
root-visible fds; the `/proc` plumbing around it is exercised in one integration
test through the real door_clients.
"""
import builtins
import io

import family

WS = 8181
WS_HEX = format(WS, "04X")          # 1FF5


def line(state, local_port, remote_port, inode):
    """One /proc/net/tcp DATA line in the kernel's exact column layout:
    sl local rem st tx:rx tr:when retrnsmt uid timeout inode  (inode == field 9)."""
    return (f"  {inode:>10}: 0100007F:{format(local_port, '04X')} "
            f"0201007F:{format(remote_port, '04X')} {state} "
            f"00000000:00000000 00:00000000 00000000 1000 0 {inode}")


EST, LISTEN, TIME_WAIT, SYN = "01", "0A", "06", "02"


# ---------------------------------------------------------------- the pure rule
def test_a_human_leg_to_the_door_counts():
    # the gateway's relayed leg: destination == ws_port, held by no process we own
    assert family._count_door_sockets(
        [line(EST, 40000, WS, "1111")], ours=set(), ws_port=WS) == 1


def test_the_keyboard_driver_leg_does_not_count():
    # the driver dials ws_port too, but its socket is one of ours -> subtracted.
    # get this wrong and vault-run never tears down: the count stays >=1 for as
    # long as the browser is up, so chromium is the ambient warm tab forever.
    assert family._count_door_sockets(
        [line(EST, 40001, WS, "2222")], ours={"2222"}, ws_port=WS) == 0


def test_a_watching_human_counts_once_even_with_the_driver_up():
    # the state vault-run runs in while a human watches the sign-in: browser up
    # (driver leg present) AND a human at the door. Humans, not total legs.
    both = [line(EST, 40001, WS, "2222"),      # driver, ours
            line(EST, 40000, WS, "1111")]      # gateway human
    assert family._count_door_sockets(both, ours={"2222"}, ws_port=WS) == 1


def test_a_leg_to_another_port_never_counts():
    # the websockify->x11vnc leg, chromium's CDP, the driver's RFB-side socket:
    # all ESTABLISHED but not on the door port, so none is a noVNC client.
    other = [line(EST, WS, 5901, "3333"),      # websockify accepted side (local=WS)
             line(EST, 40002, 9222, "4444")]   # cdp
    assert family._count_door_sockets(other, ours=set(), ws_port=WS) == 0


def test_only_established_states_count():
    # the LISTEN socket and a TIME_WAIT of a just-closed tab must not read as a
    # human: a lingering socket is exactly the phantom that must fall through.
    states = [line(LISTEN, WS, 0, "5555"),
              line(TIME_WAIT, 40000, WS, "6666"),
              line(SYN, 40003, WS, "7777")]
    assert family._count_door_sockets(states, ours=set(), ws_port=WS) == 0


def test_malformed_lines_are_skipped_not_fatal():
    junk = ["", "   header line with too few fields", line(EST, 40000, WS, "8888")]
    assert family._count_door_sockets(junk, ours=set(), ws_port=WS) == 1


# ------------------------------------------------------- the real door_clients
def test_door_clients_reads_gateway_leg_and_drops_the_driver(monkeypatch):
    """End to end through door_clients: its own-socket scan marks the driver's
    inode ours, the gateway leg (different inode) is the one human it reports,
    and a missing /proc/net/tcp6 (the container has one family) is tolerated."""
    monkeypatch.setattr(family, "proc_inodes", lambda match: {"2222"})
    tcp = line(EST, 40001, WS, "2222") + "\n" + line(EST, 40000, WS, "1111") + "\n"

    def fake_open(path, *a, **k):
        if path == "/proc/net/tcp":
            return io.StringIO("sl local rem st hdr\n" + tcp)
        raise OSError("no tcp6 in this netns")

    monkeypatch.setattr(builtins, "open", fake_open)
    assert family.door_clients(WS) == 1


def test_door_clients_with_no_leg_says_zero(monkeypatch):
    monkeypatch.setattr(family, "proc_inodes", lambda match: set())
    monkeypatch.setattr(builtins, "open",
                        lambda path, *a, **k: io.StringIO("header\n"))
    assert family.door_clients(WS) == 0
