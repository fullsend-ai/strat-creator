# Authored architecture fixture

Routing forwards serving requests through HTTP and owns client-facing timeout
configuration. No cancellation endpoint is defined. The dataset controller
manages dataset metadata and deletes objects through the storage API. Storage
operations may fail independently for each object. A legal hold must be respected.
The pipeline scheduler starts jobs using secret references. Running jobs retain
the credential resolved at launch; new launches resolve the current reference.
No automatic credential rollback or provider revocation order is specified.
There are no active overlays or real customer details in this fixture.
