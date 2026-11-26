"""Label every file with Claude Code running headless (your Claude plan, no API key).

  python scripts/label_claude.py            # all batches (resumes where it stopped)
  python scripts/label_claude.py --only 1   # just the first batch, to test

Same engine as label_ai.py, limited to the `claude` command. To let Claude share the work with
free engines (Gemini, Groq, Ollama...) and back each other up, run label_ai.py instead.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.argv[1:1] = ["--ai", "claude"]
from label_ai import main

sys.exit(main())
