module.exports = {
  testEnvironment: 'node',
  transformIgnorePatterns: ['/node_modules/(?!@cornerstonejs/)'],
  testMatch: ['**/circle-fixtures.test.js', '**/bidirectional-fixtures.test.js'],
};
