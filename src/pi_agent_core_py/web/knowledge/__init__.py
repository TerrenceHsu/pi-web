"""Knowledge Library subsystem (P2-R1 Library Foundation).

Local-only knowledge library persistence:

- 5 SQLite tables in independent ``knowledge.db`` (see ``store.py``)
- Library / Document / Ingestion Job / Chunk / Session Binding metadata
- Logical foreign keys (no SQL FK) per P2-R0 §2.1 (decision R2)

R1 scope (per ``docs/design/p2-r0-amendment-1.md``):

- ✅ Schema + migration + KnowledgeStore repository
- ✅ KnowledgeFileStore + Service (R1-B)
- ✅ Library CRUD REST API + Session Binding API (R1-C)
- ❌ No PDF parser, no chunker, no FTS5, no ``search_knowledge`` tool
  (deferred to R2-R5)

Module layout follows P2-R1 §6:

- ``models`` — enums / dataclass DTOs only (no SQLite / FastAPI deps)
- ``store`` — SQLite repository (schema init / CRUD / binding replace-all)
- ``files`` — KnowledgeFileStore (path containment + atomic write)
- ``service`` — business validation + DB/FileStore compensation
- ``api`` — FastAPI router factory (Library CRUD + Session Binding)
"""
