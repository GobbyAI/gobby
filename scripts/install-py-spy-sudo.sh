#!/bin/sh
# Install passwordless, out-of-process py-spy access for profiling the Gobby
# daemon on macOS, where py-spy needs root (SIP gates task_for_pid).
#
# Usage: sudo sh scripts/install-py-spy-sudo.sh [path/to/py-spy]
#
# Installs, idempotently:
#   /usr/local/sbin/py-spy          root-owned copy of the caller's py-spy
#   /usr/local/sbin/py-spy-record   root-owned wrapper: PID + 1-300 s only
#   /etc/sudoers.d/py-spy           NOPASSWD `py-spy dump --pid *`
#   /etc/sudoers.d/py-spy-record    NOPASSWD for the wrapper
#   /var/tmp/py-spy/                root-owned output dir for recordings
#
# The copy must be root-owned: a sudo rule on a user-writable binary lets
# anything running as that user swap it and gain root. Bare `py-spy record` is
# never allowed, because `record -- <cmd>` runs any command and `-o` writes
# any path as root; the wrapper fixes both. The dump rule's `*` also admits
# extra read-only dump flags such as --locals, which can print local variable
# values of any Python process, root-owned ones included; this is accepted for
# a single-user workstation.
set -eu

die() {
	echo "install-py-spy-sudo: $*" >&2
	exit 1
}

[ "$(id -u)" -eq 0 ] || die "run with sudo"
user="${SUDO_USER:-}"
[ -n "$user" ] && [ "$user" != root ] || die "run with sudo from your own account"
case "$user" in *[!A-Za-z0-9._-]*) die "unsupported user name: $user" ;; esac

if [ $# -ge 1 ]; then
	src="$1"
else
	home=$(dscl . -read "/Users/$user" NFSHomeDirectory | awk '{print $2}')
	src="$home/.local/bin/py-spy"
fi
[ -e "$src" ] || die "py-spy not found at $src; pass its path as the first argument"
src=$(readlink -f "$src")
[ -f "$src" ] && [ -x "$src" ] || die "not an executable file: $src"

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

install -d -o root -g wheel -m 755 /usr/local/sbin /var/tmp/py-spy
install -o root -g wheel -m 755 "$src" /usr/local/sbin/py-spy

cat >"$tmpdir/py-spy-record" <<'EOF'
#!/bin/sh
# Root-owned: accepts only a numeric PID and duration; output goes to a fresh
# mktemp dir under root-owned /var/tmp/py-spy so no caller-chosen path is written.
# Raw collapsed stacks only: on macOS py-spy runs `open` on a flamegraph SVG,
# which launches a GUI viewer on the user's screen after every recording.
set -eu
[ $# -le 2 ] || { echo "usage: py-spy-record PID [SECONDS]" >&2; exit 2; }
pid="${1:?usage: py-spy-record PID [SECONDS]}"
secs="${2:-30}"
case "$pid" in '' | *[!0-9]*) echo "bad pid" >&2; exit 2 ;; esac
case "$secs" in '' | *[!0-9]*) echo "bad seconds" >&2; exit 2 ;; esac
if [ "$secs" -lt 1 ] || [ "$secs" -gt 300 ]; then
	echo "seconds must be 1-300" >&2
	exit 2
fi
dir=$(mktemp -d /var/tmp/py-spy/run-XXXXXX)
/usr/local/sbin/py-spy record --pid "$pid" --duration "$secs" --rate 100 --gil \
	--format raw -o "$dir/profile.txt" >&2
chmod 755 "$dir"
chmod 644 "$dir/profile.txt"
echo "$dir/profile.txt"
EOF
install -o root -g wheel -m 755 "$tmpdir/py-spy-record" /usr/local/sbin/py-spy-record

# Validate each sudoers file before it lands: a broken file can disable sudo.
install_rule() {
	printf '%s\n' "$2" >"$tmpdir/$1"
	visudo -cf "$tmpdir/$1" >/dev/null || die "sudoers syntax check failed for $1"
	install -o root -g wheel -m 440 "$tmpdir/$1" "/etc/sudoers.d/$1"
}
install_rule py-spy "$user ALL=(root) NOPASSWD: /usr/local/sbin/py-spy dump --pid *"
install_rule py-spy-record "$user ALL=(root) NOPASSWD: /usr/local/sbin/py-spy-record"

echo "installed for $user:"
echo "  sudo -n /usr/local/sbin/py-spy dump --pid <daemon-pid>"
echo "  sudo -n /usr/local/sbin/py-spy-record <daemon-pid> [seconds]"
