#!/usr/bin/env bash
# Lex Fridman podcast example — single Ralph reading a large transcripts CSV.
# Ported from fast-rlm/examples/podcast.py.
#
# Drops the Kaggle Lex Fridman transcripts CSV into the workspace, then asks
# Ralph to find what the first 5 ML-researcher guests said about AGI. This
# is the "long context exploration" demo — the CSV is many MB; Ralph has to
# grep/parse rather than load it all at once.
set -euo pipefail
BUS=./bus_podcast
source "$(dirname "$0")/_lib.sh"

mkdir -p ws_podcast/data

DATA_FILE="ws_podcast/data/lex_fridman_dataset.csv"
ZIP_PATH="ws_podcast/data/lex-fridman-podcast-transcript.zip"

if [ ! -f "$DATA_FILE" ]; then
  echo "📦 Lex Fridman transcripts CSV not found at $DATA_FILE."
  echo "   Attempting to download from Kaggle..."
  if command -v curl >/dev/null 2>&1; then
    curl -L -o "$ZIP_PATH" \
      https://www.kaggle.com/api/v1/datasets/download/rajneesh231/lex-fridman-podcast-transcript \
      || { echo "❌ curl failed — Kaggle may require auth. See instructions below."; }
    if [ -f "$ZIP_PATH" ] && command -v unzip >/dev/null 2>&1; then
      unzip -o "$ZIP_PATH" -d ws_podcast/data/ >/dev/null || true
      # Kaggle's archive contains a single CSV — find and rename it.
      found=$(find ws_podcast/data -maxdepth 2 -name "*.csv" | head -n1)
      if [ -n "$found" ] && [ "$found" != "$DATA_FILE" ]; then
        mv "$found" "$DATA_FILE"
      fi
    fi
  fi
fi

if [ ! -f "$DATA_FILE" ]; then
  cat <<EOF
❌ Couldn't get the dataset automatically. Manual steps:
   1. Download from
      https://www.kaggle.com/datasets/rajneesh231/lex-fridman-podcast-transcript
      (you may need a Kaggle account / API key — see ~/.kaggle/kaggle.json).
   2. Unzip and place the CSV at:
      $DATA_FILE
   3. Re-run this script.
EOF
  exit 1
fi

bytes=$(wc -c < "$DATA_FILE" | tr -d ' ')
echo "📚 dataset ready: $DATA_FILE ($bytes bytes)"

seed_prompt ws_podcast <<'EOF'
Find what the first 5 Machine Learning guests had to say about AGI in the
Lex Fridman Podcast. Not all guests are ML guests — focus on established
researchers known for contributions in AI. Return summaries of what the
conversations were like about AGI.

Data: `data/lex_fridman_dataset.csv` (large — many MB). Each row is a
transcript with columns including guest name, episode title, and the full
transcript text.

Workflow hints (you have no shell — only file/text tools):
1. ls + read the first ~200 lines of the CSV to learn its schema.
2. Use grep to scan for guest names that are clearly ML researchers
   (Yann LeCun, Andrew Ng, Yoshua Bengio, Ian Goodfellow, Ilya Sutskever,
   Jeff Hawkins, Demis Hassabis, Stuart Russell, etc.). Pick the FIRST 5
   such guests in chronological / row order.
3. For each of those 5, grep the transcript for "AGI" mentions and read a
   window of context (~30 lines around each hit). Truncate aggressively —
   you don't need every word, just the substance of what was said.
4. Write `summaries.md` with one section per guest: name, episode, and a
   3-6 sentence summary of their AGI take.
5. Once `summaries.md` covers 5 guests, mark_done.

Stay within your iteration budget — don't try to read the whole CSV at once.
Stream and grep.
EOF

launch podcast ws_podcast

echo "🌈 launched 1 ralph (podcast). Log in ./logs/podcast.log. Ctrl-C to stop."
wait
