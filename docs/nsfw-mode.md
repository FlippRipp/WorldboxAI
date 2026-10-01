# Manual NSFW mode

Choose an optional **NSFW Model** in Settings under the current AI provider.
OpenRouter also supports an upstream-provider selection for this model.
The mode never activates automatically.

Use **NSFW mode** above the story composer to switch models for the adventure.
The alternate model handles narration and supporting text calls, including
character tracking, memories, plot planning and image-prompt writing. Image
generation continues using its existing provider. Embeddings use their existing
model with non-graphic text prepared by the alternate model.

**Retry in NSFW mode** retries the latest completed turn from its previous game
state. The original request is reused, and the previous response remains under
**AI summaries and previous attempts**. A failed request that never committed
is retried without undoing the preceding successful turn.

Switching off prepares a detailed, non-graphic summary of each affected section.
The original conversation stays visible. Normal models receive the summaries
and non-graphic context; re-enabling the mode restores access to original scenes
and archived notes. Generated context never replaces mechanical game state.

Summary preparation must succeed before normal play resumes. You can stop it
or retry after an error. Completed summaries can be expanded and edited, and
their edits survive reloading. Editing a covered source message invalidates its
summary, which is rebuilt before the next normal-model request.

For supporting modules that assemble free-form prompts, the alternate model
also prepares a non-graphic prompt before it reaches a normal model. These
representations are cached by the complete source text. Changed prompts incur
additional alternate-model calls even when the mode is off. Keep that model
configured while continuing an adventure that contains NSFW sections.

The mode, originals and summaries are stored with the adventure and participate
in snapshots, branches and undo. Older saves default to normal mode. Content
rewriting is model-generated: review summaries when continuity matters; it
cannot guarantee that another model will accept every underlying subject.
