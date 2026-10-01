// Toyota: site not chosen yet. Fill in `lookup` and set ready: true. See mercedes.js for the contract.
export const toyota = {
  id: "toyota",
  label: "Toyota options lookup",
  makes: ["toyota"],
  wmi: ["4T1", "4T3", "4T4", "5TD", "5TF", "5TE", "5TB", "2T1", "2T3", "3TM", "3TY", "JTD", "JTE", "JTM", "JTN", "JT3", "JT2", "5YF", "7MU"],
  ready: false,
  async lookup() { throw new Error("The Toyota lookup site has not been configured yet."); },
};
