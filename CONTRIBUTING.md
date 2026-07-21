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

Keep configuration writes inside `~/.config/hypr`, preserve comments where possible, and add a regression test for changes to the parser or backup behavior.
