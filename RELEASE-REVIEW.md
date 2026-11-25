# Release review

Keep the GitHub repository private until the maintainer approves publication.

- Validate an export of the current source in a fresh checkout.
- Publish an approved source snapshot with clean history. Earlier private revisions may contain files absent from the current tree.
- Review the licenses and notices of optional dependencies and any new assets before distributing them. VirtualDJ SDK headers and prebuilt provider bundles are external to this source distribution.
- Keep credentials, local libraries, generated media, and runtime data out of the release.
- Report compatibility tests separately from physical playback, provider, and listening checks.
