"""Pictures of what the machine is looking at (12, T-10).

Its own package rather than a module under ``engine`` or ``discord``, because it belongs to
neither: it computes nothing, so it is not engine code, and it knows nothing about Discord, so a
review, a script or a test can render the same picture the card shows.
"""
