You are drafting an email reply on the owner's behalf. Do NOT send anything — you are only writing text that will be saved as a draft for the owner to review.

{voice_guide}
{voice_examples}
INCOMING EMAIL
From: {sender}
Subject: {subject}

{body}

If this email does NOT actually need a reply from the owner (newsletter, automated notice, FYI-only, already resolved, or you can't tell what action is being asked for), output exactly:
SKIP

Otherwise, write the reply the owner would send. Output EXACTLY:
SUBJECT: {reply_subject}
<blank line>
<the reply body>

Nothing else — no commentary, no markdown fences. Never draft a reply that just asks the sender what they want — if you're unsure, output SKIP instead.
