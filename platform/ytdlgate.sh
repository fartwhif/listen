#!/bin/bash
# Global yt-dlp concurrency gate.
#
# Both downloaders (DrivePod's harvest, GrabbyTube's library) shell out to the
# one yt-dlp on PATH, and both hit the SAME Google session per vault. Without a
# cap they hammer it in parallel - the pattern that got the session flagged and
# is the whole reason the vault exists. The cap is global because the shared
# resource (one session per vault, and one box) is global.
#
# Mechanism: a token bucket of N descriptor files under /run/ytdl-slots. flock
# on slot k serializes takers without a daemon; a caller that cannot get a slot
# waits for one to free rather than failing, so a queue builds politely instead
# of a burst of errors. A waiting caller re-scans EVERY slot (a 0.25 s poll),
# not just the one it probed first: blocking on a single slot idles the others
# while callers queue behind it, which turns a cap of N into N-1 free slots.
# FIFO order is not guaranteed and does not matter: all takers are doing the
# same kind of work.
#
# MAX_CONCURRENT_YTDLP comes from the environment (per-program, so the pool
# process and the downloaders can be capped differently). Default 2.
set -u

SLOTS=${YTDGATE_SLOTS_DIR:-/run/ytdl-slots}
LIMIT=${MAX_CONCURRENT_YTDLP:-2}
[ "$LIMIT" -ge 1 ] 2>/dev/null || LIMIT=2
mkdir -p "$SLOTS" 2>/dev/null || { exec "$@"; }

# Slot file count can only grow; shrinking the limit is a restart affair, which
# is the right semantics for a concurrency ceiling.
i=0
while [ "$i" -lt "$LIMIT" ]; do
  [ -e "$SLOTS/$i" ] || : > "$SLOTS/$i"
  i=$((i + 1))
done

# The lock is held by this shell's fd 9 and inherited by the child, so the slot
# is released exactly when yt-dlp exits - no pid bookkeeping, no stale slots
# after a kill (the fd closes when the process family dies).
slot=$LIMIT
asked=0
while :; do
  i=0
  while [ "$i" -lt "$LIMIT" ]; do
    # fd 9 has to point AT the slot before flock can lock it: `flock -n 9 file`
    # with fd 9 closed does not lock the file, it fails (rc 1), which used to
    # make every probe fall through and serialise every taker on slot 0 - a cap
    # of 1 however high MAX_CONCURRENT_YTDLP was set.
    exec 9<>"$SLOTS/$i" || { slot=-1; break 2; }
    if flock -n 9; then
      slot=$i
      break 2
    fi
    i=$((i + 1))
  done
  [ "$slot" -lt "$LIMIT" ] && break
  if [ "$asked" -eq 0 ]; then
    echo "ytdlgate: at the cap ($LIMIT), waiting for a slot" >&2
    asked=1
  fi
  sleep 0.25
done
exec "$@"
