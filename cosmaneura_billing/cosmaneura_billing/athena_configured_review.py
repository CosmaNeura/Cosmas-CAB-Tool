"""Configured source workflow over existing review logic and one scoped DB unit."""
from contextlib import contextmanager

from .athena_client import AthenaStaffReadClient, AthenaClientError, build_athena_staff_read_url
from .athena_review import AthenaReviewService, detached, digest, require, now


def source_binding(scope):
    p = scope.profile
    return {"schemaVersion":"athena-source-binding.v1", "serverPracticeId":p.server_practice_id,
        "chaseId":scope.case_id, "environment":p.environment, "vendorPracticeId":p.vendor_practice_id,
        "sourcePatientId":scope.source_patient_id, "connectionRevision":p.connection_revision,
        "bindingRevision":scope.binding_revision, "profileSha256":p.fingerprint,"scopeSha256":scope.fingerprint}


class _SingleUnitStore:
    """Request-local facade: nested review methods never open another connection."""
    durable = True
    def __init__(self, unit, actor, operation):
        self.unit_value, self.actor, self.operation = unit, actor, operation
        self.practice_id = unit.practice

    @contextmanager
    def unit(self, chase_id, actor, operation_id=None):
        require(chase_id == self.unit_value.chase_id and actor == self.actor
                and operation_id in (None, self.operation), "not_authorized")
        yield self.unit_value


class ConfiguredAthenaReviewService:
    mode, durable = "configured", True

    def __init__(self, store, *, credential_environment, client_id="", client_secret="",
                 discovery_key=None, clock=now, client_factory=None):
        self.store, self.practice_id, self.clock = store, store.practice_id, clock
        self.environment, self.key = credential_environment, discovery_key
        self._factory = client_factory or (lambda scope,lease=None: AthenaStaffReadClient(
            scope=scope, credential_environment=credential_environment,
            client_id=client_id, client_secret=client_secret,read_lease=lease))

    def _connection(self, unit, view):
        scope = unit.bound_scope
        binding = source_binding(scope) if scope else None
        matches = bool(binding and view.get("sourceBinding") == binding
            and view.get("caseBinding") == digest([self.practice_id,unit.patient_ref]))
        production = bool(binding and binding["environment"]=="production")
        admission = unit.read_lease.public if production and unit.read_lease else None
        if production: matches = bool(matches and admission and view.get("sourceAdmission")==admission)
        if not binding:
            state = unit.binding_error if unit.binding_error != "binding_invalid" else "binding_stale"
        elif production and admission is None: state = "read_not_admitted"
        elif self.environment != binding["environment"]: state = "credential_environment_mismatch"
        else: state = "ready" if matches or view["revision"] == 0 else "changed"
        if view.get("schemaVersion")=="athena-case-review.v3" and not production:
            state,matches="read_not_admitted",False
        if state in {"ready","changed"}:
            try: build_athena_staff_read_url(scope,"patient")  # Pure client-owned cohort validation.
            except AthenaClientError: state = "read_not_admitted"
        return {"workflow":"configured","state":state,"binding":binding,
                "readAllowed":state in {"ready","changed"},"currentReviewMatchesBinding":matches,
                **({"admission":admission} if production else {})}

    def _view(self, unit):
        view = unit.current()
        if view["revision"] and view.get("caseBinding") != digest([self.practice_id,unit.patient_ref]):
            return {"schemaVersion":"athena-case-review.v3" if view["schemaVersion"]=="athena-case-review.v3" else "athena-case-review.v2","chaseId":unit.chase_id,
                "revision":view["revision"],"reviewId":None,"candidate":None,"applied":None,
                "billingImport":None,"redacted":True}
        return view

    def _envelope(self, unit, receipt=None, *, replayed=False):
        view = self._view(unit)
        result = {"view":view,"connection":self._connection(unit,view)}
        if receipt:
            saved = receipt["payload"]
            result["operationReceipt"] = {"operationId":receipt["operationId"],"revision":saved["revision"],
                "action":receipt["action"],"replayed":replayed,"current":saved["revision"] == view["revision"],
                "sourceBinding":saved.get("sourceBinding"),
                **({"sourceAdmission":saved["sourceAdmission"]} if "sourceAdmission" in saved else {})}
        return detached(result)

    def current(self, chase_id, actor):
        with self.store.unit(chase_id,actor,resolve_binding=True,allow_missing_binding=True) as unit:
            return self._envelope(unit)

    def _logic(self, unit, actor, operation=None, intent=None):
        connection = self._connection(unit,unit.current())
        require(connection["readAllowed"],"stale_review")
        client=self._factory(unit.bound_scope,unit.read_lease) if unit.read_lease else self._factory(unit.bound_scope)
        return AthenaReviewService(_SingleUnitStore(unit,actor,operation),client,
            clock=self.clock,discovery_key=self.key,source_binding=connection["binding"],intent_override=intent,
            source_admission=connection.get("admission"),source_scope=unit.bound_scope)

    def _command(self, kind, command):
        require(type(command) is dict)
        command = dict(command)
        # Keep the legacy command identity rules: only these existing optional
        # fields were normalized. A sourcePatientId key is never erased.
        if kind == "apply" and command.get("selectedPhoneCountry") is None:
            command.pop("selectedPhoneCountry",None)
        if kind == "billing_apply" and command.get("discoveryToken") is None:
            command.pop("discoveryToken",None)
        return command

    def _replay(self, unit, operation, intent):
        receipt = unit.receipt(operation)
        if receipt:
            require(receipt["intent"] == intent,"operation_conflict")
            require(receipt["payload"].get("caseBinding") == digest([self.practice_id,unit.patient_ref]),"stale_review")
            receipt["operationId"] = operation
        return receipt

    def _mutate(self, kind, chase_id, actor, command, audit):
        command = self._command(kind,command)
        intent = AthenaReviewService(self.store,None)._intent(kind,chase_id,actor,command)
        operation = command["operationId"]
        # This short unit closes before any registry lock is acquired. Exact
        # committed operations recover without active mapping or network work.
        with self.store.unit(chase_id,actor,operation) as unit:
            receipt = self._replay(unit,operation,intent)
        with self.store.unit(chase_id,actor,operation,resolve_binding=True,allow_missing_binding=True) as unit:
            receipt = self._replay(unit,operation,intent)
            if receipt:
                unit.audit(audit,actor,"athena."+kind)
                return self._envelope(unit,receipt,replayed=True)
            connection = self._connection(unit,unit.current())
            require(connection["readAllowed"],"stale_review")
            if kind == "refresh":
                production=connection["binding"]["environment"]=="production"
                require(set(command)=={"operationId","expectedRevision","expectedBindingSha256"}|({"expectedAdmissionSha256"} if production else set()))
                require(command["expectedBindingSha256"] == connection["binding"]["scopeSha256"],"stale_review")
                if production:require(command["expectedAdmissionSha256"]==connection["admission"]["grantSha256"],"stale_review")
                inner = {"operationId":operation,"expectedRevision":command["expectedRevision"],
                         "sourcePatientId":connection["binding"]["sourcePatientId"]}
            else:
                require(connection["currentReviewMatchesBinding"],"stale_review")
                inner = command
            logic = self._logic(unit,actor,operation,intent)
            getattr(logic,kind)(chase_id,actor,inner,audit=audit)
            saved = unit.receipt(operation)
            require(saved is not None,"source_unavailable")
            saved["operationId"] = operation
            return self._envelope(unit,saved)

    def refresh(self,chase_id,actor,command,*,audit=None): return self._mutate("refresh",chase_id,actor,command,audit)
    def apply(self,chase_id,actor,command,*,audit=None): return self._mutate("apply",chase_id,actor,command,audit)
    def billing_apply(self,chase_id,actor,command,*,audit=None): return self._mutate("billing_apply",chase_id,actor,command,audit)

    def billing_read(self,chase_id,actor,**kwargs):
        with self.store.unit(chase_id,actor,resolve_binding=True,allow_missing_binding=True) as unit:
            require(self._connection(unit,unit.current())["currentReviewMatchesBinding"],"stale_review")
            return self._logic(unit,actor).billing_read(chase_id,actor,**kwargs)
