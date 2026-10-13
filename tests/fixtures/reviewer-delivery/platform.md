# Authored architecture fixture

The routing component forwards requests to the serving component through HTTP.
The serving component executes the requested work and returns a response.
The routing component owns request timeout configuration and client-facing errors.
This fixture does not define a cancellation endpoint or guarantee that closing
an HTTP connection stops work at the serving component. There are no other
components, platform versions or active overlays in this fixture.
