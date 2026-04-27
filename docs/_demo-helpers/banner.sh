# Prints a muted title banner (gray comment style) before the demo starts.
# Pack-cli-demo.tape sources this inside Hide right before Show, so the first
# recorded frame shows: banner + blank prompt + first command typing.
printf '\e[38;5;244m# anywhere-agents pack · v0.5.0\e[0m\n\n'
