# failed

- Manually counting character positions in the ASCII grid produced two wrong puzzle strings (cells misidentified) before the tool-assisted parse fixed it — manual re-parsing of the same grid is error-prone and should be skipped.
- Attempting sudoku_solver directly with hand-parsed strings before verifying the parse wasted two tool calls and added confusion.
- Running parse_helper without first verifying it accepts the new puzzle (it was hardcoded) wasted one tool call and returned a completely wrong grid.
