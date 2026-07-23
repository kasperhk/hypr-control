# Contributing

Bug reports and focused pull requests are welcome. Please describe the Omarchy and Hyprland versions you tested and include the output of `hyprctl configerrors` when reporting configuration problems.

## Development

Run the app directly from a clone:

```bash
./hypr-control-center
```

Run the test suite before submitting a change:

```bash
make test
```

Keep Hyprland configuration writes inside `~/.config/hypr`, preserve comments
where possible, and keep persistent application data inside the appropriate XDG
directory. Display discovery, identity, profile matching, and geometry belong
in the GUI-independent `hypr_control_center.displays` package. Add regression
tests for parser, backup, profile, or layout behavior.
