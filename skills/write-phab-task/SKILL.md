---
description: Generate a Phabricator task in the standard WMF markdown format. Use when the user wants to draft a new task with a title, summary, optional technical notes, and acceptance criteria.
disable-model-invocation: false
argument-hint: "[component] [description]"
---

# Write a Phabricator Task

Generate a Phabricator task in markdown using the template below. Output the task wrapped in a fenced markdown code block so the engineer can copy/paste it cleanly into Phabricator.

## Rules

- Do NOT use line breaks within sections (let the editor wrap text naturally).
- Do NOT add leading spaces to any lines.
- The "Technical notes" section is optional — include it only if there are meaningful technical details.
- Acceptance criteria should have 1–3 items. More than that means the task is too broad.
- **Output the entire task wrapped in a ```` ```markdown ```` fenced block.** No commentary outside the fence.

## Template

Use this exact structure (wrapped in a fenced ```` ```markdown ```` block in your final output):

```
Title: {component}: Short description

## Summary

1-2 sentence summary of the task.

## Technical notes

(Optional section with notes about technical approach.)

## Acceptance criteria

- [ ] Item 1
- [ ] Item 2
```

## Input

The user's arguments are: `$ARGUMENTS`

Parse the first word as the component and the rest as the description. Use them to fill in the template above.
