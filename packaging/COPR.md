# Publishing to Copr (Fedora) and extensions.gnome.org

Two artifacts, two channels: the daemon + extension RPMs go through
[Copr](https://copr.fedorainfracloud.org/), and the extension alone also goes
to [extensions.gnome.org](https://extensions.gnome.org) (E.G.O) for people who
install extensions the GNOME way. They are independent — a user can take
either or both.

## Copr

One-time setup:

1. Create the project at <https://copr.fedorainfracloud.org/coprs/> — name
   `ai-token-monitor`, chroots `fedora-42-x86_64` and `fedora-rawhide-x86_64`
   (`noarch`, so one arch is enough; add others only if asked for).
2. `sudo dnf install copr-cli rpm-build` and get an API token from
   <https://copr.fedorainfracloud.org/api/> into `~/.config/copr`.

Each release:

```console
$ git tag -a v0.5.0 -m "..." && git push --tags   # Source0 points at the tag
$ make srpm                                       # -> packaging/*.src.rpm
$ make copr                                       # copr-cli build ai-token-monitor <srpm>
```

`Source0` is the GitHub tarball for the tag in `Version:`, so **tag first** —
`rpmbuild` will not find the source otherwise. Bump `Version:` and add a
`%changelog` entry in `packaging/ai-token-monitor.spec` in the same commit that
bumps `daemon/pyproject.toml`, `daemon/src/ai_token_monitor/__init__.py` and
`extension/metadata.json` (`version-name`).

Users then install with:

```console
$ sudo dnf copr enable theyisuskill/ai-token-monitor
$ sudo dnf install ai-token-monitor gnome-shell-extension-ai-token-monitor
```

### Checking the payload before you publish

The extension subpackage must ship **five** things — miss one and the
extension installs but is quietly broken (this happened through v0.4.0, which
shipped without `prefs.js`, `icons/` and the translations):

```console
$ rpmbuild -bb packaging/ai-token-monitor.spec --define "_sourcedir $PWD"
$ rpm -qlp ~/rpmbuild/RPMS/noarch/gnome-shell-extension-ai-token-monitor-*.rpm
```

Expect `extension.js`, `prefs.js`, `metadata.json`, `stylesheet.css`,
`icons/*.svg` and `locale/*/LC_MESSAGES/*.mo`. `make install-extension` is the
reference for what a working install contains — the spec has to match it.

## extensions.gnome.org

```console
$ make pack     # -> ai-token-monitor@theyisuskill.github.io.shell-extension.zip
```

Upload at <https://extensions.gnome.org/upload/>. What reviewers check, and
where this extension stands:

- **No bundled binaries, no network of its own.** The extension is a pure D-Bus
  client; every byte of parsing and every outbound request lives in the daemon.
  Say so in the submission notes — it is the point in this design that most
  often gets questioned.
- **It needs a separate daemon to show anything.** State it in the description
  (`metadata.json`) *and* in the notes, with the Copr command. An extension
  that renders "Daemon offline" until you install something else will be
  rejected if that isn't obvious up front.
- **Cleanup in `disable()`.** `AITokenMonitorExtension.disable()` destroys the
  indicator, which cancels the `Gio.Cancellable`, removes both GLib timeouts
  and disconnects the D-Bus signal and name-owner handlers.
- **`shell-version`.** Only claim versions you have actually run. Drop any you
  can't test rather than guessing — a broken version claim is a takedown.
- **GPL-compatible license**: GPL-3.0-or-later, and `icons/NOTICE` records the
  provenance of the bundled simple-icons SVGs (CC0).
