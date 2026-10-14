/** Keep focus in the shadow root when a submitted control becomes disabled. */
export function focusSubmissionStatus(form, status) {
  const active = form.getRootNode?.()?.activeElement;
  if (active && form.contains(active)) status.focus();
}

/** Persistent live feedback remains available throughout processing. */
export function configureSubmissionStatus(status) {
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.setAttribute("aria-atomic", "true");
  status.setAttribute("tabindex", "-1");
}
