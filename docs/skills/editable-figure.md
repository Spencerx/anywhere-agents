# editable-figure

Design a paper, proposal, or README figure and deliver it as a PowerPoint file whose text, shapes, and connectors the author can still edit. Covers source analysis, reference search for the document type, layout for one takeaway, native object construction, and inspection at the real publication width. Complements `ci-mockup-figure`, which produces HTML-captured and code-native figures instead.

!!! warning "Completing a build needs desktop PowerPoint"

    A PPTX library can write native objects on a headless machine, so authoring itself is not the constraint. Validation is: nothing on a Linux server, container, or CI job can confirm the file opens, renders, and edits as intended, which is the check that separates a real editable figure from a flattened image. That check needs desktop PowerPoint, so Windows or macOS. `scripts/render_powerpoint.ps1` narrows further, using Windows COM automation with no macOS equivalent. Assessment and prompt-only requests are unaffected. When the deliverable can be an image, [`ci-mockup-figure`](ci-mockup-figure.md) covers a headless machine.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#fdf5f6', 'primaryBorderColor': '#8b2635', 'primaryTextColor': '#1a1a1a', 'lineColor': '#8b2635'}}}%%
flowchart LR
    A([source section<br/>+ placement]) --> B[1. analyze<br/>reader takeaway]
    B --> C[2. find references<br/>paper / README / proposal]
    C --> D[3. design for<br/>the takeaway]
    D --> E[4. build native<br/>objects in PPTX]
    E --> F[5. inspect meaning,<br/>space, editability]
    F -->|issue found| D
    F -->|clean| G([.pptx + .pdf / .png<br/>in the project])
```

The three inspection checks are separate on purpose. A figure can render correctly and still overstate a claim. It can also look right in a preview while its text is a flattened image rather than an editable object.

{%
   include-markdown "../../skills/editable-figure/SKILL.md"
   start="## Overview"
%}
