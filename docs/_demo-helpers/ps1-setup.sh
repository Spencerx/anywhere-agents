# Custom PS1 for the pack-cli-demo recording.
# Mauve ❯ prompt (Catppuccin Mocha 256-color 141) + reset + space.
# \001 / \002 mark non-printing regions for readline width calc.
PS1=$'\001\e[38;5;141m\002❯\001\e[0m\002 '
export PS1
