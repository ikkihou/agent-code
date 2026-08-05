# Reminder

## Items for optimization

1. rendering effect for "ask_question"
2. the agent ask for user's permission for every "python" bash tool use, which is inconvenient
3. ~~the output text in the output_area should be copiable~~ (done: click output to focus → drag select → Ctrl+C copies; Ctrl+C with no selection copies whole output)
4. add "thinking" animation when agent is working background

## Items for fix Immediately

1. ~~the auto scroll down function of "output_area" doesn't work as expected~~ (done: follow now based on render_info.bottom_visible, resumes when scrolled back to bottom; End jumps to bottom)
2. ~~bizzare output truncation when agent output its "final" trace~~ (done: empty completions retried once; final rebuilt from streamed text when the final message omits text)

## Feature to be implemented

1. add "/clear" command for clearing all messages in the current session
