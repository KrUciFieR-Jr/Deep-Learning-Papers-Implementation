import tensorflow as tf
import numpy as np

from util import get_batch_data
from config import FLAGS

epsilon = 1e-9


def _squashing(vector):
    vector_squared_norm = tf.reduce_sum(tf.square(vector), axis=-2, keep_dims=True)
    scalar_factor = vector_squared_norm / ((1 + vector_squared_norm) * tf.sqrt(vector_squared_norm + epsilon))
    vector_squash = scalar_factor * vector  # element-wise
    return vector_squash


def _routing(input, b_IJ):
    W = tf.get_variable('weight', shape=(1, input.shape[1].value, FLAGS.num_classes, input.shape[-2].value, 16),
                        dtype=tf.float32, initializer=tf.random_normal_initializer(stddev=FLAGS.stddev))
    input = tf.tile(input, [1, 1, 10, 1, 1])
    W = tf.tile(W, [FLAGS.batch_size, 1, 1, 1, 1])
    assert input.get_shape() == [FLAGS.batch_size, 1152, 10, 8, 1]
    u_hat = tf.matmul(W, input, transpose_a=True)
    assert u_hat.get_shape() == [FLAGS.batch_size, 1152, 10, 16, 1]
    for r_iter in range(FLAGS.iter_routing):
        with tf.variable_scope('routing_iter_' + str(r_iter)):
            c_IJ = tf.nn.softmax(b_IJ, dim=2)
            assert c_IJ.get_shape() == [FLAGS.batch_size, 1152, 10, 1, 1]
            s_J = tf.multiply(c_IJ, u_hat)
            s_J = tf.reduce_sum(s_J, axis=1, keep_dims=True)
            assert s_J.get_shape() == [FLAGS.batch_size, 1, 10, 16, 1]
            v_J = _squashing(s_J)
            assert v_J.get_shape() == [FLAGS.batch_size, 1, 10, 16, 1]
            v_J_tile = tf.tile(v_J, [1, 1152, 1, 1, 1])
            u_v = tf.matmul(u_hat, v_J_tile, transpose_a=True)
            assert u_v.get_shape() == [FLAGS.batch_size, 1152, 10, 1, 1]
            if r_iter < FLAGS.iter_routing - 1:
                b_IJ += u_v
    return v_J


class CapsLayer(object):
    def __init__(self, output_number, vec_length, layer_type):
        self.output_number = output_number
        self.vec_length = vec_length
        self.layer_type = layer_type

    def __call__(self, input):
        if self.layer_type == 'CONV':
            assert input.get_shape() == [FLAGS.batch_size, 20, 20, 256]
            capsules = tf.contrib.layers.conv2d(input, self.output_number * self.vec_length, 9, 2, padding='VALID')
            capsules = tf.reshape(capsules, [FLAGS.batch_size, -1, self.vec_length, 1])
            capsules = _squashing(capsules)
            assert capsules.get_shape() == [FLAGS.batch_size, 1152, 8, 1]
            return capsules
        elif self.layer_type == 'FC':
            with tf.variable_scope('routing'):
                self.input = tf.reshape(input, shape=(FLAGS.batch_size, -1, 1, input.shape[-2].value, 1))
                b_IJ = tf.constant(np.zeros([FLAGS.batch_size, input.shape[1].value, self.output_number, 1, 1], dtype=np.float32))
                capsules = _routing(self.input, b_IJ)
                capsules = tf.squeeze(capsules, axis=1)
            return capsules


class CapsuleNet(object):
    def __init__(self, is_training=True):
        self.graph = tf.Graph()
        with self.graph.as_default():
            if is_training:
                self.image, self.label = get_batch_data(is_training=True)
                self.one_hot_label = tf.one_hot(self.label, depth=10, axis=1, dtype=tf.float32)
                self._build_arch()
                self._loss()
                self._summary()

                self.global_step = tf.Variable(0, name='global_step', trainable=False)
                self.optimizer = tf.train.AdamOptimizer()
                self.train_op = self.optimizer.minimize(self.total_loss, global_step=self.global_step)
            else:
                if FLAGS.mask_with_y:
                    self.image = tf.placeholder(tf.float32, shape=(FLAGS.batch_size, 28, 28, 1))
                    self.one_hot_label = tf.placeholder(tf.float32, shape=(FLAGS.batch_size, 10, 1))
                    self._build_arch()
                else:
                    self.image = tf.placeholder(tf.float32, shape=(FLAGS.batch_size, 28, 28, 1))
                    self._build_arch()
        tf.logging.info('Seting up the main structure')

    def _build_arch(self):
        with tf.variable_scope('RELU_CONV_1'):
            conv1 = tf.contrib.layers.conv2d(self.image, num_outputs=256, kernel_size=9, stride=1,
                                             padding='VALID')
            assert conv1.get_shape() == [FLAGS.batch_size, 20, 20, 256]
        with tf.variable_scope('PrimaryCaps_layers'):
            primaryCaps = CapsLayer(output_number=32, vec_length=8, layer_type='CONV')
            caps1 = primaryCaps(conv1)
            assert caps1.get_shape() == [FLAGS.batch_size, 1152, 8, 1]
        with tf.variable_scope('DigitCaps_Layers'):
            digitCaps = CapsLayer(output_number=10, vec_length=16, layer_type='FC')
            self.caps2 = digitCaps(caps1)
        with tf.variable_scope('Making'):
            self.v_length = tf.sqrt(tf.reduce_sum(tf.square(self.caps2), axis=2, keep_dims=True) + epsilon)
            self.softmax_v = tf.nn.softmax(self.v_length, dim=1)
            assert self.softmax_v.get_shape() == [FLAGS.batch_size, 10, 1, 1]
            self.argmax_idx = tf.to_int32(tf.argmax(self.softmax_v, axis=1))
            assert self.argmax_idx.get_shape() == [FLAGS.batch_size, 1, 1]
            self.argmax_idx = tf.reshape(self.argmax_idx, shape=(FLAGS.batch_size,))
            if not FLAGS.mask_with_y:
                masked_v = []
                for batch_size in range(FLAGS.batch_size):
                    v = self.caps2[batch_size][self.argmax_idx[batch_size], :]
                    masked_v.append(tf.reshape(v, shape=(1, 1, 16, 1)))
                self.masked_v = tf.concat(masked_v, axis=0)
                assert self.masked_v.get_shape() == [FLAGS.batch_size, 1, 16, 1]
            else:
                self.masked_v = tf.matmul(tf.squeeze(self.caps2),
                                          tf.reshape(self.one_hot_label, (-1, 10, 1)), transpose_a=True)
                self.v_length = tf.sqrt(tf.reduce_sum(tf.square(self.caps2), axis=2, keep_dims=True)
                                        + epsilon)
        with tf.variable_scope('Reconstruct'):
            v_j = tf.reshape(self.masked_v, shape=(FLAGS.batch_size, -1))
            fc1 = tf.contrib.layers.fully_connected(inputs=v_j, num_outputs=512)
            assert fc1.get_shape() == [FLAGS.batch_size, 512]
            fc2 = tf.contrib.layers.fully_connected(inputs=fc1, num_outputs=1024)
            assert fc2.get_shape() == [FLAGS.batch_size, 1024]
            self.reconstruct = tf.contrib.layers.fully_connected(inputs=fc2,
                                                                 num_outputs=784,
                                                                 activation_fn=tf.sigmoid)

    def _loss(self):
        max_l = tf.square(tf.maximum(0., FLAGS.m_plus - self.v_length))
        max_r = tf.square(tf.maximum(0., self.v_length - FLAGS.m_minus))
        assert max_r.get_shape() == [FLAGS.batch_size, 10, 1, 1]

        max_l = tf.reshape(max_l, shape=(FLAGS.batch_size, -1))
        max_r = tf.reshape(max_r, shape=(FLAGS.batch_size, -1))

        T_c = self.one_hot_label
        L_c = T_c * max_l + FLAGS.lambda_val * (1 - T_c) * max_r
        self.margin_loss = tf.reduce_mean(tf.reduce_mean(L_c, axis=1))

        image_true = tf.reshape(self.image, shape=(FLAGS.batch_size, -1))
        self.reconstruct_loss = tf.reduce_mean(tf.square(self.reconstruct - image_true))
        self.total_loss = self.margin_loss + FLAGS.regularization_scale * self.reconstruct_loss

    def _summary(self):
        train_summary = []
        train_summary.append(tf.summary.scalar('train/margin_loss', self.margin_loss))
        train_summary.append(tf.summary.scalar('train/reconstruction_loss', self.reconstruct_loss))
        train_summary.append(tf.summary.scalar('train/total_loss', self.total_loss))
        recon_img = tf.reshape(self.reconstruct, shape=(FLAGS.batch_size, 28, 28, 1))
        train_summary.append(tf.summary.image('reconstruction_img', recon_img))
        self.train_summary = tf.summary.merge(train_summary)

        correct_prediction = tf.equal(tf.to_int32(self.label), self.argmax_idx)
        self.batch_accuracy = tf.reduce_sum(tf.cast(correct_prediction, tf.float32))
        self.test_acc = tf.placeholder_with_default(tf.constant(0.), shape=[])

