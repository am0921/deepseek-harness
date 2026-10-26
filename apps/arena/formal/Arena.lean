import Std

/-! Kernel-checked properties of the experiment controller and the Redis subset model.
These theorems concern the definitions below. Python, Docker, the OS and external
providers remain outside this proof; conformance is tested separately. -/

namespace Arena

inductive Phase where
  | preparing | ready | running | grading | feedback | passed | error | stopped | limit
  deriving DecidableEq, Repr

inductive Signal where
  | prepared | start | idle | feedback | ok | resume | error | stop | limit
  deriving DecidableEq, Repr

def next : Phase → Signal → Option Phase
  | .preparing, .prepared => some .ready
  | .ready, .start => some .running
  | .running, .idle => some .grading
  | .grading, .feedback => some .feedback
  | .grading, .ok => some .passed
  | .feedback, .resume => some .running
  | .preparing, .error | .ready, .error | .running, .error
  | .grading, .error | .feedback, .error => some .error
  | .preparing, .stop | .ready, .stop | .running, .stop
  | .grading, .stop | .feedback, .stop => some .stopped
  | .running, .limit | .grading, .limit | .feedback, .limit => some .limit
  | _, _ => none

theorem grading_only_after_idle (p : Phase) (s : Signal)
    (h : next p s = some .grading) : p = .running ∧ s = .idle := by
  cases p <;> cases s <;> simp_all [next]

theorem feedback_only_after_grading (p : Phase) (s : Signal)
    (h : next p s = some .feedback) : p = .grading ∧ s = .feedback := by
  cases p <;> cases s <;> simp_all [next]

theorem pass_only_after_grading (p : Phase) (s : Signal)
    (h : next p s = some .passed) : p = .grading ∧ s = .ok := by
  cases p <;> cases s <;> simp_all [next]

theorem terminal_absorbing (p : Phase) (s : Signal)
    (h : p = .passed ∨ p = .error ∨ p = .stopped ∨ p = .limit) : next p s = none := by
  rcases h with h | h | h | h <;> subst p <;> cases s <;> rfl

def release (leftReady rightReady : Bool) : Bool := leftReady && rightReady

theorem release_requires_both (l r : Bool) (h : release l r = true) : l = true ∧ r = true := by
  cases l <;> cases r <;> simp_all [release]

def allocation (lane width : Nat) : List Nat := (List.range width).map (lane * width + ·)

theorem allocations_equal_size (lane width : Nat) : (allocation lane width).length = width := by
  simp [allocation]

theorem allocations_disjoint (width cpu : Nat)
    (left : cpu ∈ allocation 0 width) (right : cpu ∈ allocation 1 width) : False := by
  simp [allocation] at left right
  obtain ⟨i, hi, heq⟩ := right
  omega

structure Entry where
  value : Int
  deadline : Option Nat
  deriving DecidableEq, Repr

def visible (e : Entry) (now : Nat) : Bool :=
  match e.deadline with
  | none => true
  | some deadline => now < deadline

def setValue (value : Int) : Entry := ⟨value, none⟩

def increment (e : Entry) : Except Unit Entry :=
  if -(2^63 : Int) ≤ e.value ∧ e.value < (2^63 : Int) - 1 then
    .ok ⟨e.value + 1, e.deadline⟩
  else .error ()

theorem set_clears_deadline (v : Int) : (setValue v).deadline = none := rfl

theorem increment_preserves_deadline (e e' : Entry) (h : increment e = .ok e') :
    e'.deadline = e.deadline := by
  unfold increment at h
  split at h <;> simp_all
  cases h
  rfl

theorem overflow_rejected (e : Entry) (h : e.value ≥ (2^63 : Int) - 1) :
    increment e = .error () := by
  simp [increment]
  omega

theorem exact_expiry_boundary (e : Entry) (d : Nat) (h : e.deadline = some d) :
    visible e d = false := by
  simp [visible, h]

theorem expired_stays_expired (e : Entry) (d t₁ t₂ : Nat)
    (hd : e.deadline = some d) (h₁ : d ≤ t₁) (h₂ : t₁ ≤ t₂) : visible e t₂ = false := by
  simp [visible, hd]
  omega

theorem no_deadline_stays_visible (e : Entry) (t : Nat) (h : e.deadline = none) :
    visible e t = true := by
  simp [visible, h]

def bill (input cached output inputRate cachedRate outputRate : Nat) : Nat :=
  (input - cached) * inputRate + cached * cachedRate + output * outputRate

theorem cached_input_partition (input cached : Nat) (h : cached ≤ input) :
    input - cached + cached = input := Nat.sub_add_cancel h

theorem equal_rates_no_double_count (input cached output rate outputRate : Nat)
    (h : cached ≤ input) : bill input cached output rate rate outputRate =
      input * rate + output * outputRate := by
  unfold bill
  rw [← Nat.add_mul, Nat.sub_add_cancel h]

theorem zero_cache_bill (input output ri rc ro : Nat) :
    bill input 0 output ri rc ro = input * ri + output * ro := by
  simp [bill]

def phaseName : Phase → String
  | .preparing => "preparing" | .ready => "ready" | .running => "running"
  | .grading => "grading" | .feedback => "feedback" | .passed => "passed"
  | .error => "error" | .stopped => "stopped" | .limit => "limit"

def signalName : Signal → String
  | .prepared => "prepared" | .start => "start" | .idle => "idle"
  | .feedback => "feedback" | .ok => "pass" | .resume => "resume"
  | .error => "error" | .stop => "stop" | .limit => "limit"

def phases : List Phase := [.preparing, .ready, .running, .grading, .feedback, .passed, .error, .stopped, .limit]
def signals : List Signal := [.prepared, .start, .idle, .feedback, .ok, .resume, .error, .stop, .limit]

theorem all_phases (p : Phase) : p ∈ phases := by cases p <;> decide
theorem all_signals (s : Signal) : s ∈ signals := by cases s <;> decide

end Arena

#print axioms Arena.grading_only_after_idle
#print axioms Arena.feedback_only_after_grading
#print axioms Arena.pass_only_after_grading
#print axioms Arena.terminal_absorbing
#print axioms Arena.release_requires_both
#print axioms Arena.allocations_equal_size
#print axioms Arena.allocations_disjoint
#print axioms Arena.set_clears_deadline
#print axioms Arena.increment_preserves_deadline
#print axioms Arena.overflow_rejected
#print axioms Arena.exact_expiry_boundary
#print axioms Arena.expired_stays_expired
#print axioms Arena.no_deadline_stays_visible
#print axioms Arena.cached_input_partition
#print axioms Arena.equal_rates_no_double_count
#print axioms Arena.zero_cache_bill
#print axioms Arena.all_phases
#print axioms Arena.all_signals

def main : IO Unit := do
  for p in Arena.phases do
    for s in Arena.signals do
      if let some result := Arena.next p s then
        IO.println s!"EDGE {Arena.phaseName p} {Arena.signalName s} {Arena.phaseName result}"
