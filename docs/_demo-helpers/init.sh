# One-shot init for the pack-cli-demo recording.
# Called from pack-cli-demo.tape's Hide block. Order matters:
#   1. isolate user-level config to /tmp/aa-demo
#   2. set up mauve ❯ prompt (PS1)
#   3. clear — wipes the visible `source /tmp/init.sh` command text
#   4. print banner — stays as top-of-screen title throughout the demo
export HOME=/tmp/aa-demo
mkdir -p "$HOME"
source /tmp/ps1-setup.sh
clear
source /tmp/banner.sh
