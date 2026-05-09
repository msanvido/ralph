# worked

- Writing a Python backtracking solver tool (tools/sudoku_solver.py) and invoking it immediately produced the correct solution in one shot.
- Parsing the ASCII grid manually into an 81-char string before calling the solver avoided any format-confusion errors.
- Writing both the puzzle and solution into solution.md in a single write call kept the output clean and complete.
- Reusing the existing sudoku_solver.py tool from iteration 1 saved time — no need to rewrite it.
- Writing a parse_helper.py tool to programmatically extract cell values from the ASCII grid resolved the manual mis-parsing that caused two 'No solution found' errors.
- Calling parse_helper first to get the confirmed 81-char string, then passing it to sudoku_solver, produced the correct solution immediately.
- Manually parsing the grid row-by-row (reading each cell directly from the Unicode box-drawing characters) was accurate and fast when the parse_helper was hardcoded to an old puzzle.
- Calling sudoku_solver immediately with the hand-parsed 81-char string succeeded in one shot — no 'No solution found' error — confirming the parse was correct.
- Checking ls output first revealed that parse_helper was hardcoded to a previous puzzle, avoiding a wasted tool call with wrong output.
