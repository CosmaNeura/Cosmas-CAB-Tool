import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { readAthenaEnvelope, readAthenaView, readAthenaBilling, mergeAthenaBilling, athenaSourceReadFields, athenaConnectionMessage, athenaImportMessage } from './athenaCaseReview.js';
const fixture=JSON.parse(readFileSync(new URL('./athenaReview.fixture.json',import.meta.url)));
const clone=(value)=>structuredClone(value);
const binding=()=>({schemaVersion:'athena-source-binding.v1',serverPracticeId:'local-configured-proof',chaseId:fixture.chaseId,
  environment:'preview',vendorPracticeId:'195900',sourcePatientId:'60178',connectionRevision:1,bindingRevision:1,
  profileSha256:'a'.repeat(64),scopeSha256:'b'.repeat(64)});
const view=()=>({ ...clone(fixture.source), schemaVersion:'athena-case-review.v2',mode:'preview',sourceBinding:binding(),
  candidate:{...clone(fixture.source.candidate),sourceBinding:binding()} });
const envelope=(v=view())=>({view:v,connection:{workflow:'configured',state:'ready',readAllowed:true,currentReviewMatchesBinding:true,binding:binding()}});
const command={path:'/api/athena/card-chase/read',body:{operationId:'11111111-1111-4111-8111-111111111111'}};
const receipt=()=>({operationId:command.body.operationId,revision:1,action:'athena.refresh',replayed:false,current:true,sourceBinding:binding()});

test('configured initial review sends only the stored binding CAS, never a patient override',()=>{
  const e=envelope({schemaVersion:'athena-case-review.v1',chaseId:fixture.chaseId,revision:0,reviewId:null,candidate:null,applied:null});
  e.connection.currentReviewMatchesBinding=false;
  const value=readAthenaEnvelope(e,fixture.chaseId);
  assert.deepEqual(athenaSourceReadFields(value.connection,'60184'),{expectedBindingSha256:binding().scopeSha256});
  assert.deepEqual(athenaSourceReadFields(null,'60178'),{sourcePatientId:'60178'});
});

test('v2 source identity is bound at view and candidate while v1 remains readable',()=>{
  assert.equal(readAthenaEnvelope(envelope(),fixture.chaseId).view.candidate.source.patientId,'60178');
  assert.equal(readAthenaEnvelope({view:fixture.source},fixture.chaseId).connection,null);
  assert.throws(()=>readAthenaEnvelope({view:fixture.source},fixture.chaseId,null,true));
  for (const mutate of [e=>delete e.connection,e=>delete e.view.sourceBinding,e=>delete e.view.candidate.sourceBinding,
    e=>e.view.candidate.sourceBinding.scopeSha256='c'.repeat(64),e=>e.connection.binding.sourcePatientId='60179',
    e=>e.connection.binding.serverPracticeId='another-practice',e=>e.connection.binding.adminApproved=true,
    e=>e.connection.binding.bindingRevision=true]) {
    const e=envelope();mutate(e);assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId));
  }
});

test('changed mapping retains older source as history and requires a fresh read',()=>{
  const e=envelope();e.connection={...e.connection,state:'changed',currentReviewMatchesBinding:false,
    binding:{...binding(),sourcePatientId:'60179',bindingRevision:2,scopeSha256:'c'.repeat(64)}};
  const parsed=readAthenaEnvelope(e,fixture.chaseId);
  assert.equal(parsed.view.candidate.source.patientId,'60178');
  assert.deepEqual(athenaSourceReadFields(parsed.connection,''),{expectedBindingSha256:'c'.repeat(64)});
  assert.match(athenaConnectionMessage(parsed.connection),/mapping.*changed.*again/);
});

test('missing and revoked mappings are explicit unavailable states; production identity never enables a read',()=>{
  for (const state of ['connection_missing','connection_revoked','binding_missing','binding_revoked','binding_stale','read_not_admitted','credential_environment_mismatch']) {
    const e=envelope();e.connection={...e.connection,state,readAllowed:false,currentReviewMatchesBinding:false,binding:null};
    assert.ok(athenaConnectionMessage(readAthenaEnvelope(e,fixture.chaseId).connection));
    assert.throws(()=>athenaSourceReadFields(e.connection,'60178'));
  }
  const e=envelope();e.connection={...e.connection,state:'read_not_admitted',readAllowed:false,currentReviewMatchesBinding:false,
    binding:{...binding(),environment:'production',vendorPracticeId:'1234',sourcePatientId:'901'},admission:null};
  assert.equal(readAthenaEnvelope(e,fixture.chaseId).connection.binding.environment,'production');
  e.connection.state='ready';e.connection.readAllowed=true;
  assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId));
});

test('redacted current view retains CAS revision but cannot carry former patient source',()=>{
  const e=envelope({schemaVersion:'athena-case-review.v2',chaseId:fixture.chaseId,revision:7,reviewId:null,candidate:null,applied:null,billingImport:null,redacted:true});
  e.connection.currentReviewMatchesBinding=false;
  assert.equal(readAthenaEnvelope(e,fixture.chaseId).view.revision,7);
  assert.match(athenaConnectionMessage(e.connection,true),/case patient changed/i);
  for (const extra of [{sourceBinding:binding()},{candidate:fixture.source.candidate},{applied:fixture.applied}])
    assert.throws(()=>readAthenaEnvelope({...e,view:{...e.view,...extra}},fixture.chaseId));
});

test('operation-specific replay binds the submitted command while returning current state',()=>{
  const e=envelope();e.operationReceipt=receipt();
  assert.equal(readAthenaEnvelope(e,fixture.chaseId,command).receipt.current,true);
  assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId));
  assert.throws(()=>readAthenaEnvelope(envelope(),fixture.chaseId,command));
  e.view.revision=3;e.operationReceipt={...receipt(),current:false,replayed:true};
  assert.equal(readAthenaEnvelope(e,fixture.chaseId,command).view.revision,3);
  for (const delta of [{operationId:'22222222-2222-4222-8222-222222222222'},{action:'athena.apply'},{current:true},{revision:4}])
    assert.throws(()=>readAthenaEnvelope({...e,operationReceipt:{...e.operationReceipt,...delta}},fixture.chaseId,command));
});

test('current applied observations require the current binding; older carried observations stay readable',()=>{
  const v={...view(),revision:2,applied:clone(fixture.applied)};
  assert.throws(()=>readAthenaView({view:v},fixture.chaseId));
  v.applied.sourceBinding=binding();
  assert.equal(readAthenaView({view:v},fixture.chaseId).applied.consent,'unknown');
  v.reviewId='22222222-2222-4222-8222-222222222222';delete v.applied.sourceBinding;
  assert.ok(readAthenaView({view:v},fixture.chaseId).applied);
});

test('billing and discovery cannot reuse another mapping even for equal patient/source values',()=>{
  const v={...view(),revision:2,applied:{...clone(fixture.applied),sourceBinding:binding()}};
  const result={...clone(fixture.billing),mode:'preview',sourceBinding:binding()};
  assert.ok(readAthenaBilling(result,v,'1','88001').note);
  for (const delta of [{sourceBinding:undefined},{sourceBinding:{...binding(),scopeSha256:'c'.repeat(64)}}])
    assert.throws(()=>readAthenaBilling({...result,...delta},v,'1','88001'));
  const first=mergeAthenaBilling(null,{...result,note:null},v,'1');
  const changed=clone(v);changed.sourceBinding.scopeSha256='c'.repeat(64);
  assert.throws(()=>mergeAthenaBilling(first,result,changed,'1',{encounter:'88001'}),{status:409});
  assert.equal(first.note,null);
});


test('unavailable registered Preview identities are visible without acquiring cohort read authority',()=>{
  for (const delta of [{sourcePatientId:'60185'},{vendorPracticeId:'195901'}]) {
    for (const v of [view(),{schemaVersion:'athena-case-review.v1',chaseId:fixture.chaseId,revision:0,reviewId:null,candidate:null,applied:null}]) {
      const e=envelope(v);e.connection={...e.connection,state:'read_not_admitted',readAllowed:false,currentReviewMatchesBinding:false,
        binding:{...binding(),...delta}};
      assert.deepEqual(readAthenaEnvelope(e,fixture.chaseId).connection.binding,e.connection.binding);
      assert.match(athenaConnectionMessage(e.connection),/aren’t enabled.*administrator/);
      assert.throws(()=>athenaSourceReadFields(e.connection,'60178'));
      e.connection.state='ready';e.connection.readAllowed=true;
      assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId));
      assert.throws(()=>athenaSourceReadFields(e.connection,'60178'));
    }
    const sourced=view();Object.assign(sourced.sourceBinding,delta);sourced.candidate.sourceBinding=clone(sourced.sourceBinding);
    Object.assign(sourced.candidate.source,{patientId:sourced.sourceBinding.sourcePatientId,vendorPracticeId:sourced.sourceBinding.vendorPracticeId});
    assert.throws(()=>readAthenaView({view:sourced},fixture.chaseId));
  }
  for (const delta of [{sourcePatientId:'0'},{vendorPracticeId:null},{sourcePatientId:'01'},{environment:'other'}]) {
    const e=envelope();e.connection={...e.connection,state:'read_not_admitted',readAllowed:false,currentReviewMatchesBinding:false,binding:{...binding(),...delta}};
    assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId));
  }
});

const productionBinding=()=>({...binding(),environment:'production',vendorPracticeId:'1234',sourcePatientId:'901',scopeSha256:'d'.repeat(64)});
const admission=()=>({schemaVersion:'athena-read-storage-admission.v1',admissionId:'33333333-3333-4333-8333-333333333333',
  admissionRevision:1,scopeSha256:productionBinding().scopeSha256,grantSha256:'e'.repeat(64),expiresAt:'2099-01-01T00:00:00Z'});
const production=()=>{
  const v=view();v.schemaVersion='athena-case-review.v3';v.mode='production';v.sourceBinding=productionBinding();v.sourceAdmission=admission();
  v.candidate={...v.candidate,schemaVersion:'athena-card-chase-review.v2',sourceBinding:productionBinding(),sourceAdmission:admission(),
    source:{...v.candidate.source,system:'athena_production',syntheticOnly:false,dataClass:'operational',vendorPracticeId:'1234',patientId:'901'}};
  const e=envelope(v);e.connection={...e.connection,binding:productionBinding(),admission:admission()};return e;
};

test('Production source work carries the exact read/storage grant without model authority',()=>{
  const e=production(),parsed=readAthenaEnvelope(e,fixture.chaseId);
  assert.equal(parsed.view.candidate.source.syntheticOnly,false);
  assert.deepEqual(athenaSourceReadFields(parsed.connection,'60178'),{expectedBindingSha256:productionBinding().scopeSha256,expectedAdmissionSha256:admission().grantSha256});
  assert.match(athenaImportMessage({source:'athena_staff_review_operational'}),/Open the encounter to check coding availability/);
  assert.equal(athenaImportMessage(fixture.import),'Reviewed note imported into CAB.');
  const prior=envelope();prior.connection={...e.connection,currentReviewMatchesBinding:false,state:'changed'};
  assert.equal(readAthenaEnvelope(prior,fixture.chaseId).view.mode,'preview');
});

test('Production provenance rejects missing, cross-scope, invented or synthetic-labelled admission',()=>{
  for (const mutate of [e=>delete e.connection.admission,e=>e.connection.admission=null,e=>delete e.view.sourceAdmission,
    e=>delete e.view.candidate.sourceAdmission,e=>e.view.sourceAdmission.scopeSha256='f'.repeat(64),
    e=>e.view.candidate.sourceAdmission.grantSha256='f'.repeat(64),e=>e.connection.admission.admissionRevision=true,
    e=>e.connection.admission.expiresAt='not-a-date',e=>e.connection.admission.modelApproved=true,
    e=>e.view.candidate.source.syntheticOnly=true,e=>e.view.candidate.source.system='athena_preview',
    e=>e.view.candidate.source.dataClass='synthetic',e=>e.view.candidate.source.patientId='902',
    e=>e.view.schemaVersion='athena-case-review.v2',e=>delete e.connection]) {
    const e=production();mutate(e);assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId));
  }
  const unavailable=production();unavailable.connection={...unavailable.connection,state:'read_not_admitted',readAllowed:false,currentReviewMatchesBinding:false,admission:null};
  assert.ok(readAthenaEnvelope(unavailable,fixture.chaseId));
  assert.throws(()=>athenaSourceReadFields(unavailable.connection,''));
});

test('renewal needs fresh review while expired original receipts remain recoverable history',()=>{
  const e=production();e.connection.admission={...admission(),admissionRevision:2,grantSha256:'f'.repeat(64)};
  assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId));
  e.connection.state='changed';e.connection.currentReviewMatchesBinding=false;
  assert.deepEqual(athenaSourceReadFields(readAthenaEnvelope(e,fixture.chaseId).connection,''),{expectedBindingSha256:productionBinding().scopeSha256,expectedAdmissionSha256:'f'.repeat(64)});
  e.view.sourceAdmission.expiresAt='2020-01-01T00:00:00Z';e.view.candidate.sourceAdmission=clone(e.view.sourceAdmission);e.view.revision=3;
  e.operationReceipt={...receipt(),replayed:true,current:false,sourceBinding:productionBinding(),sourceAdmission:clone(e.view.sourceAdmission)};
  assert.equal(readAthenaEnvelope(e,fixture.chaseId,command).receipt.sourceAdmission.expiresAt,'2020-01-01T00:00:00Z');
  delete e.operationReceipt.sourceAdmission;assert.throws(()=>readAthenaEnvelope(e,fixture.chaseId,command));
});

test('Production applied/imported history retains its own proof and cannot masquerade as Preview',()=>{
  const e=production();e.view.revision=2;
  e.view.applied={...clone(fixture.applied),sourceBinding:productionBinding(),sourceAdmission:admission()};
  e.view.billingImport={...clone(fixture.import),source:'athena_staff_review_operational',sourcePatientId:'901',sourceBinding:productionBinding(),sourceAdmission:admission()};
  assert.equal(readAthenaEnvelope(e,fixture.chaseId).view.billingImport.source,'athena_staff_review_operational');
  for (const mutate of [v=>delete v.applied.sourceAdmission,v=>delete v.billingImport.sourceBinding,
    v=>v.billingImport.sourceAdmission.scopeSha256='f'.repeat(64),v=>v.billingImport.source='athena_staff_review_preview_synthetic']) {
    const copy=clone(e);mutate(copy.view);assert.throws(()=>readAthenaEnvelope(copy,fixture.chaseId));
  }
  e.view.reviewId='22222222-2222-4222-8222-222222222222';e.view.applied=clone(fixture.applied);e.view.billingImport=clone(fixture.import);
  assert.ok(readAthenaEnvelope(e,fixture.chaseId).view.applied);
  const redacted=production();redacted.view={schemaVersion:'athena-case-review.v3',chaseId:fixture.chaseId,revision:4,reviewId:null,candidate:null,applied:null,billingImport:null,redacted:true};
  redacted.connection.currentReviewMatchesBinding=false;assert.equal(readAthenaEnvelope(redacted,fixture.chaseId).view.revision,4);
});

test('Production discovery/selected note is grant-bound even when patient and mapping are unchanged',()=>{
  const e=production(),v=e.view;v.revision=2;v.applied={...clone(fixture.applied),sourceBinding:productionBinding(),sourceAdmission:admission()};
  const result={...clone(fixture.billing),mode:'production',sourcePatientId:'901',sourceBinding:productionBinding(),sourceAdmission:admission()};
  assert.ok(readAthenaBilling(result,v,'1','88001').note);
  const first=mergeAthenaBilling(null,{...result,note:null},v,'1');
  const changed=clone(v);changed.sourceAdmission.grantSha256='f'.repeat(64);
  assert.throws(()=>mergeAthenaBilling(first,result,changed,'1',{encounter:'88001'}),{status:409});
  delete result.sourceAdmission;assert.throws(()=>readAthenaBilling(result,v,'1','88001'));
});
