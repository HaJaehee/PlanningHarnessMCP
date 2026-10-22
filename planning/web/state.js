// Approval page - what the page keeps between polls, all in one place. Each field names
// the code that changes it, so a value that looks wrong has few places to come from.
const state={
  busy:false,      // a decision or a control is on its way - decide(), control()
  pendingCount:0,  // undecided requests, for the flashing tab title - poll()
  lastError:'',    // why the last click could not be recorded - decide(), control()
  runs:[],         // the running plans, as of the last poll - poll()
  // Request id -> its phase - restore(), restoreRun(). Both phases can be reviewed task
  // by task, but the request means different things: on a plan it rewrites the task's
  // wording, on a completion report it orders that one task to be done again. The phase
  // is kept so the buttons can say which of the two the human is about to ask for.
  phase:{},
};
